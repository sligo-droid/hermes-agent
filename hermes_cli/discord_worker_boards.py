"""Discord thread boards for durable Codex worker sessions.

This module is deliberately control-plane only. It creates and mutates Kanban
state for Discord project threads, but it does not call Hermes inference and it
does not expose Hermes tools to workers.
"""

from __future__ import annotations

import contextlib
import html
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import quote, urlsplit, urlunsplit

from hermes_cli import kanban_db
from hermes_cli.discord_time import discord_message_exceeds_age_limit
from hermes_cli.discord_thread_context import (
    DISCORD_CONTEXT_KIND_SINGLE_MESSAGE,
    discord_context_quality_from_text,
    expand_discord_thread_references,
    format_discord_thread_expansions,
    has_discord_thread_reference,
)
from hermes_cli.discord_plan_artifacts import lookup_discord_plan_artifact
from hermes_cli.discord_worker_roles import (
    BOARD_RUN_SUMMARY_FILENAME,
    DEV_TICKET_BODY_GUIDANCE,
    DISCORD_WORKER_DISPATCH_DIRTY_FILENAME,
    DISCORD_WORKER_META_KEY,
    GOAL_CONTROL_COMMANDS,
    PUBLIC_BOARD_COLUMNS,
    PUBLIC_TOKEN_BYTES,
    REVIEW_LOOP_CONTINUE_EXTRA_LOOPS,
    REVIEW_LOOP_LIMIT_BLOCKED_REASON,
    ROLE_ASSIGNEES,
    ROLE_DEV,
    ROLE_FOREMAN,
    ROLE_PLANNER,
    ROLE_REVIEWER,
    TERMINAL_GOAL_STATUSES,
    active_dev_round,
    board_slug_for_discord_request,
    board_slug_for_discord_thread,
    format_role_round_title,
)
from hermes_cli.discord_worker_state import (
    CODEX_STATE_LOG_TAIL_BYTES,
    cap_state_value as _cap_state_value,
    codex_worker_state_path,
    read_codex_worker_state as _read_codex_worker_state,
    record_codex_worker_event,
    record_codex_worker_result,
)
from agent.runtime_breakdown import render_runtime_breakdown_text
from utils import atomic_json_write


logger = logging.getLogger(__name__)
DEFAULT_REVIEW_LOOP_LIMIT = 5
FOREMAN_REVIEW_LOOP_LIMIT = 3
BOARD_RUN_SUMMARY_SCHEMA_VERSION = 3
PR_OPEN_POLICY_AFTER_REVIEW_APPROVAL = "after_review_approval"
PR_OPEN_POLICY_NEVER = "never"
MERGE_POLICY_AUTO = "auto"
MERGE_POLICY_MANUAL = "manual"
MERGE_POLICY_NEVER = "never"
VALID_MERGE_POLICIES = frozenset({MERGE_POLICY_AUTO, MERGE_POLICY_MANUAL, MERGE_POLICY_NEVER})
VALID_PR_OPEN_POLICIES = frozenset({PR_OPEN_POLICY_AFTER_REVIEW_APPROVAL, PR_OPEN_POLICY_NEVER})
_DISCORD_MESSAGE_URL_RE = re.compile(
    r"https?://(?:canary\.|ptb\.)?discord(?:app)?\.com/channels/"
    r"(?P<guild>\d+)/(?P<channel>\d+)/(?P<message>\d+)"
)
_DISCORD_MESSAGE_ID_RE = re.compile(
    r"\b(?:message|msg)\s+(?P<message>\d{16,24})\b",
    re.IGNORECASE,
)
_SOURCE_TASK_ERROR_OUTCOMES = frozenset({"spawn_failed", "crashed", "timed_out", "gave_up"})
_DELETE_META = object()
_POSIX_PATH_RE = re.compile(
    r"(?<![\w:/.-])/(?:home|Users|tmp|var|etc|opt|private|workspace|workspaces|mnt|srv|repo|root)"
    r"(?:/[^\s\"'<>),;{}\[\]]*)?"
)
_WINDOWS_PATH_RE = re.compile(r"(?<![\w:/.-])[A-Za-z]:\\[^\s\"'<>),;{}\[\]]+")
_CONTEXT_PACK_JSON_FILENAME = "context-pack.json"
_CONTEXT_PACK_MARKDOWN_FILENAME = "context-pack.md"
_SKIPPED_BOARD_TARGET_LOG_KEYS: set[tuple[str, str, str]] = set()
_BOARD_METADATA_LOCK_TIMEOUT_SECONDS = 5.0
_BOARD_METADATA_LOCK_POLL_SECONDS = 0.05
_PRE_REVIEW_MAX_TASKS = 5
_PRE_REVIEW_MAX_LIST_ITEMS = 8
_PRE_REVIEW_MAX_TEXT_CHARS = 800
_PRE_REVIEW_SECRET_KEY_RE = re.compile(r"token|secret|password|api[_-]?key|auth|credential", re.IGNORECASE)
_PR_FINALIZER_RECOVERY_ROUTE_TEXT_RE = re.compile(
    r"delegate_coding_task\s*\(\s*route_decision|"
    r"ui_visual_specialist|"
    r"z-ai/glm-5\.2|"
    r"selected_provider\s*=\s*openrouter|"
    r"selected_model\s*=\s*z-ai/glm-5\.2",
    re.IGNORECASE,
)
_PR_FINALIZER_RECOVERY_NEUTRAL_ROOT_GOAL = (
    "Recover PR finalization for the already approved implementation by addressing the current PR blocker."
)


class TicketMoveConflict(RuntimeError):
    """Raised when a ticket status move is valid syntax but refused."""


class BoardMetadataLockTimeout(TimeoutError):
    """Raised when a board metadata RMW lock cannot be acquired in time."""


def _now() -> int:
    return int(time.time())


def _is_foreman_generated_request(request: object) -> bool:
    text = str(request or "").lstrip()
    return text.startswith("Foreman escalation:") or text.startswith("/goal Foreman escalation:")


def pr_policy_for_request(request: object) -> dict[str, str]:
    """Infer board-level PR policy from the user's request.

    Ambiguous implementation requests should take the normal Sligo path:
    open a PR after reviewer approval, wait for checks, then merge. Explicit
    review-only / do-not-merge wording overrides that default.
    """
    text = re.sub(r"\s+", " ", str(request or "")).strip().casefold()
    pr_open_policy = PR_OPEN_POLICY_AFTER_REVIEW_APPROVAL
    merge_policy = MERGE_POLICY_AUTO
    if _request_forbids_pr_lifecycle(text):
        pr_open_policy = PR_OPEN_POLICY_NEVER
        merge_policy = MERGE_POLICY_NEVER
    elif _request_forbids_merge(text):
        merge_policy = MERGE_POLICY_NEVER
    elif _request_requires_manual_merge(text):
        merge_policy = MERGE_POLICY_MANUAL
    return {
        "pr_open_policy": pr_open_policy,
        "merge_policy": merge_policy,
    }


def effective_pr_policy_for_worker(worker: dict[str, Any]) -> dict[str, str]:
    """Resolve PR policy from durable board intent, not only stale metadata."""

    context = worker.get("project_context") if isinstance(worker.get("project_context"), dict) else {}
    github_pr_amend = context.get("github_pr_amend") if isinstance(context.get("github_pr_amend"), dict) else {}
    if (
        github_pr_amend.get("requires_head_sha_advance") is True
        and str(github_pr_amend.get("head_repo") or "").strip()
        and str(github_pr_amend.get("head_ref") or "").strip()
    ):
        return {
            "pr_open_policy": PR_OPEN_POLICY_AFTER_REVIEW_APPROVAL,
            "merge_policy": MERGE_POLICY_AUTO,
        }

    text_parts: list[str] = []
    for key in ("root_goal", "initial_request", "latest_planner_request"):
        value = str(worker.get(key) or "").strip()
        if value:
            text_parts.append(value)
    for item in worker.get("criteria") or []:
        if isinstance(item, dict):
            if item.get("active", True):
                value = str(item.get("text") or "").strip()
                if value:
                    text_parts.append(value)
        else:
            value = str(item or "").strip()
            if value:
                text_parts.append(value)
    for item in worker.get("requirements") or []:
        if isinstance(item, dict):
            value = str(item.get("text") or "").strip()
            if value:
                text_parts.append(value)

    inferred = pr_policy_for_request("\n".join(text_parts))
    if inferred["pr_open_policy"] == PR_OPEN_POLICY_NEVER:
        return inferred

    pr_open_policy = str(worker.get("pr_open_policy") or PR_OPEN_POLICY_AFTER_REVIEW_APPROVAL).strip().lower()
    if pr_open_policy not in VALID_PR_OPEN_POLICIES:
        pr_open_policy = PR_OPEN_POLICY_AFTER_REVIEW_APPROVAL
    merge_policy = str(worker.get("merge_policy") or inferred["merge_policy"]).strip().lower()
    if merge_policy not in VALID_MERGE_POLICIES:
        merge_policy = inferred["merge_policy"]
    if inferred["merge_policy"] == MERGE_POLICY_NEVER:
        merge_policy = MERGE_POLICY_NEVER
    elif inferred["merge_policy"] == MERGE_POLICY_MANUAL and merge_policy == MERGE_POLICY_AUTO:
        merge_policy = MERGE_POLICY_MANUAL
    return {"pr_open_policy": pr_open_policy, "merge_policy": merge_policy}


def _request_forbids_pr_lifecycle(text: str) -> bool:
    if not text:
        return False
    patterns = (
        r"\bdo\s+not\s+open\s+(?:pull\s+requests?|prs?)\b",
        r"\bdon['’]?t\s+open\s+(?:pull\s+requests?|prs?)\b",
        r"\bwithout\s+opening\s+(?:a\s+)?(?:pull\s+request|pr)\b",
        r"\bno\s+(?:pull\s+requests?|prs?)\b",
        r"\bno\s+pr\s+lifecycle\b",
        r"\blocal[-\s]?only\b.{0,160}\b(?:no|without|does\s+not)\b.{0,80}\b(?:pull\s+requests?|prs?|push|merge|remote\s+checks?)\b",
        r"\blocal\s+verified\b.{0,160}\b(?:no|without|does\s+not)\b.{0,80}\b(?:pull\s+requests?|prs?|push|merge|remote\s+checks?)\b",
        r"\bdev\s+work\s+stops\s+at\s+(?:a\s+)?local\s+verified\s+branch\s+state\b",
        r"\bdoes\s+not\s+open\s+pull\s+requests?\b.{0,120}\bpush\b.{0,120}\bmerge\b",
    )
    return any(re.search(pattern, text) for pattern in patterns)


def _request_forbids_merge(text: str) -> bool:
    if not text:
        return False
    patterns = (
        r"\bdo\s+not\s+merge\b",
        r"\bdon['’]?t\s+merge\b",
        r"\bdont\s+merge\b",
        r"\bwithout\s+merging\b",
        r"\bunmerged\b",
        r"\bopen\s+(?:a\s+)?(?:pull\s+request|pr)\s+only\b",
        r"\b(?:pull\s+request|pr)\s+only\b",
        r"\bleave\s+(?:the\s+)?(?:pull\s+request|pr)\s+open\b",
        r"\bkeep\s+(?:the\s+)?(?:pull\s+request|pr)\s+open\b",
        r"\bclose\s+(?:the\s+)?(?:pull\s+request|pr)\b",
        r"\bopen\s+(?:a\s+)?(?:pull\s+request|pr)\b.{0,80}\b(?:do\s+not|don['’]?t|dont)\s+merge\b",
        r"\b(?:do\s+not|don['’]?t|dont)\s+land\b",
        r"\breview[-\s]?only\b",
    )
    return any(re.search(pattern, text) for pattern in patterns)


def _request_requires_manual_merge(text: str) -> bool:
    if not text:
        return False
    patterns = (
        r"\bmanual\s+merge\b",
        r"\bmerge\s+manually\b",
        r"\bdo\s+not\s+auto[-\s]?merge\b",
        r"\bno\s+auto[-\s]?merge\b",
        r"\bwait\s+for\s+(?:human\s+)?(?:approval|review)\s+before\s+merg",
    )
    return any(re.search(pattern, text) for pattern in patterns)


def active_dev_round_for_board(board: Optional[str]) -> int:
    if not board:
        return 1
    return active_dev_round(_read_worker_meta(board))


def _metadata_path(board: str) -> Path:
    return kanban_db.board_metadata_path(board)


@contextlib.contextmanager
def _board_metadata_lock(
    metadata_path: Path,
    *,
    timeout_seconds: float = _BOARD_METADATA_LOCK_TIMEOUT_SECONDS,
):
    """Serialize read-modify-write updates to a board metadata JSON file."""

    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = metadata_path.with_name(metadata_path.name + ".lock")
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
                    raise BoardMetadataLockTimeout(str(lock_path)) from exc
                time.sleep(_BOARD_METADATA_LOCK_POLL_SECONDS)
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


def board_run_summary_path(board: str) -> Path:
    """Return the deterministic terminal summary sidecar path for a board."""
    return kanban_db.board_dir(board) / BOARD_RUN_SUMMARY_FILENAME


def context_pack_path(board: str) -> Path:
    return kanban_db.board_dir(board) / _CONTEXT_PACK_JSON_FILENAME


def context_pack_markdown_path(board: str) -> Path:
    return kanban_db.board_dir(board) / _CONTEXT_PACK_MARKDOWN_FILENAME


def _context_pack_digest(
    root_goal: str,
    request: str,
    thread_context: str,
    plan_artifacts: Optional[list[dict[str, Any]]] = None,
    context_quality: Optional[dict[str, Any]] = None,
) -> str:
    payload = json.dumps(
        {
            "root_goal": str(root_goal or ""),
            "request": str(request or ""),
            "discord_thread_context": str(thread_context or ""),
            "plan_artifacts": _normalize_discord_plan_artifacts(plan_artifacts),
            "discord_context_quality": dict(context_quality or {}),
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _context_pack_message_count(thread_context: str) -> int:
    lines = [line for line in str(thread_context or "").splitlines() if line.strip()]
    return sum(1 for line in lines if line.lstrip().startswith("[") and not line.startswith("[Goal thread context"))


def _context_pack_source_message_ids(text: str) -> list[str]:
    seen: set[str] = set()
    ids: list[str] = []
    for match in re.finditer(r"\b\d{16,24}\b", str(text or "")):
        value = match.group(0)
        if value not in seen:
            seen.add(value)
            ids.append(value)
    return ids


def _normalize_discord_plan_artifacts(value: Any) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in (value if isinstance(value, list) else []):
        if not isinstance(item, dict):
            continue
        path = str(item.get("artifact_path") or "").strip()
        artifact_id = str(item.get("artifact_id") or "").strip()
        content_sha256 = str(item.get("content_sha256") or "").strip()
        source_url = str(item.get("source_url") or "").strip()
        thread_id = str(item.get("thread_id") or "").strip()
        channel_id = str(item.get("channel_id") or "").strip()
        source_message_id = str(item.get("source_message_id") or "").strip()
        bot_message_ids = [str(mid).strip() for mid in item.get("bot_message_ids") or [] if str(mid).strip()]
        matched_identifier = str(item.get("matched_identifier") or "").strip()
        key = artifact_id or path or content_sha256
        if not key or key in seen:
            continue
        seen.add(key)
        artifacts.append(
            {
                "artifact_id": artifact_id,
                "artifact_path": path,
                "content_sha256": content_sha256,
                "kind": str(item.get("kind") or "discord_plan"),
                "created_at": str(item.get("created_at") or ""),
                "updated_at": str(item.get("updated_at") or ""),
                "thread_id": thread_id,
                "channel_id": channel_id,
                "source_message_id": source_message_id,
                "source_url": source_url,
                "bot_message_ids": bot_message_ids,
                "matched_identifier": matched_identifier,
            }
        )
    return artifacts


def _clean_artifact_path_candidate(value: str) -> str:
    return str(value or "").strip().rstrip(".:")


def _looks_like_plan_artifact_path(value: str) -> bool:
    path = _clean_artifact_path_candidate(value)
    if not path:
        return False
    lowered = path.replace("\\", "/").casefold()
    suffix_ok = lowered.endswith((".md", ".markdown", ".txt"))
    return suffix_ok and (
        "/plans/" in lowered
        or "/artifacts/discord-plans/" in lowered
        or lowered.endswith("/plans/readme.md")
    )


def _plan_artifact_path_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()
    for regex in (_POSIX_PATH_RE, _WINDOWS_PATH_RE):
        for match in regex.finditer(str(text or "")):
            value = _clean_artifact_path_candidate(match.group(0))
            if not value or value in seen or not _looks_like_plan_artifact_path(value):
                continue
            seen.add(value)
            candidates.append(value)
    return candidates


def _local_plan_artifact_from_path(path: str) -> Optional[dict[str, Any]]:
    value = _clean_artifact_path_candidate(path)
    if not value or not _looks_like_plan_artifact_path(value):
        return None
    try:
        resolved = Path(value).expanduser()
        if not resolved.is_absolute() or not resolved.is_file():
            return None
        content_sha256 = hashlib.sha256(resolved.read_bytes()).hexdigest()
    except OSError:
        return None
    return {
        "artifact_id": "",
        "artifact_path": str(resolved),
        "content_sha256": content_sha256,
        "kind": "local_plan",
        "created_at": "",
        "updated_at": "",
        "thread_id": "",
        "channel_id": "",
        "source_message_id": "",
        "source_url": "",
        "bot_message_ids": [],
        "matched_identifier": value,
    }


def _artifact_context_texts(request: str, worker: dict[str, Any]) -> list[str]:
    texts: list[str] = [str(request or "")]
    for key in (
        "root_goal",
        "initial_request",
        "latest_planner_request",
        "latest_goal_thread_context",
    ):
        value = str(worker.get(key) or "").strip()
        if value:
            texts.append(value)
    for item in worker.get("criteria") or []:
        if isinstance(item, dict):
            value = str(item.get("text") or item.get("title") or "").strip()
        else:
            value = str(item or "").strip()
        if value:
            texts.append(value)
    for item in worker.get("requirements") or []:
        if isinstance(item, dict):
            value = str(item.get("text") or "").strip()
        else:
            value = str(item or "").strip()
        if value:
            texts.append(value)
    return texts


def _format_plan_artifact_markdown(item: dict[str, Any]) -> str:
    path = str(item.get("artifact_path") or "").strip()
    artifact_id = str(item.get("artifact_id") or "").strip()
    source_url = str(item.get("source_url") or "").strip()
    label = path or artifact_id or source_url
    suffix = []
    if artifact_id:
        suffix.append(f"artifact_id={artifact_id}")
    if source_url:
        suffix.append(f"source={source_url}")
    return f"- {label}" + (f" ({'; '.join(suffix)})" if suffix else "")


def render_context_pack_markdown(pack: dict[str, Any]) -> str:
    warnings = [str(item) for item in pack.get("warnings") or [] if str(item).strip()]
    source_ids = [str(item) for item in pack.get("source_message_ids") or [] if str(item).strip()]
    plan_artifacts = _normalize_discord_plan_artifacts(pack.get("plan_artifacts"))
    plan_artifact_lines = [_format_plan_artifact_markdown(item) for item in plan_artifacts]
    context_quality = (
        pack.get("discord_context_quality") if isinstance(pack.get("discord_context_quality"), dict) else {}
    )
    lines = [
        "# Discord Goal Context Pack",
        "",
        f"Version: {pack.get('version') or 1}",
        f"Updated at: {pack.get('updated_at') or ''}",
        f"Truncated: {bool(pack.get('truncated'))}",
        f"Message count: {int(pack.get('message_count') or 0)}",
        f"Discord context kind: {context_quality.get('kind') or 'none'}",
        f"Discord degraded context: {bool(context_quality.get('degraded'))}",
        "",
        "## Root Goal",
        str(pack.get("root_goal") or ""),
        "",
        "## Request",
        str(pack.get("request") or ""),
        "",
        "## Source Message IDs",
        ", ".join(source_ids) if source_ids else "None detected.",
        "",
        "## Durable Discord Plan Artifacts",
        "\n".join(plan_artifact_lines) if plan_artifact_lines else "None detected.",
        "",
        "## Warnings",
        "\n".join(f"- {item}" for item in warnings) if warnings else "None.",
        "",
        "## Discord Thread Context",
        str(pack.get("discord_thread_context") or ""),
        "",
    ]
    return "\n".join(lines)


def read_context_pack(board: str) -> dict[str, Any]:
    try:
        with context_pack_path(board).open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
        return payload if isinstance(payload, dict) else {}
    except OSError:
        return {}
    except json.JSONDecodeError:
        return {}


def _context_pack_summary(board: str, pack: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    data = dict(pack or read_context_pack(board) or {})
    if not data and not context_pack_path(board).exists():
        return {}
    return {
        "json_path": str(context_pack_path(board)),
        "markdown_path": str(context_pack_markdown_path(board)),
        "version": int(data.get("version") or 1),
        "truncated": bool(data.get("truncated")),
        "warnings": [str(item) for item in data.get("warnings") or [] if str(item).strip()],
        "message_count": int(data.get("message_count") or 0),
        "source_message_ids": [
            str(item) for item in data.get("source_message_ids") or [] if str(item).strip()
        ],
        "plan_artifacts": _normalize_discord_plan_artifacts(data.get("plan_artifacts")),
        "discord_context_quality": dict(data.get("discord_context_quality") or {}),
    }


def write_context_pack(
    board: str,
    *,
    root_goal: str,
    request: str,
    thread_context: str,
    plan_artifacts: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    existing = read_context_pack(board)
    normalized_artifacts = _normalize_discord_plan_artifacts(plan_artifacts)
    context_quality = discord_context_quality_from_text(thread_context)
    digest = _context_pack_digest(
        root_goal,
        request,
        thread_context,
        normalized_artifacts,
        context_quality,
    )
    version = int(existing.get("version") or 0) if existing else 0
    if existing.get("content_digest") != digest:
        version += 1
    elif version <= 0:
        version = 1
    truncated = str(thread_context or "").lstrip().startswith("[Goal thread context truncated")
    warnings = ["Discord thread context was truncated to recent messages."] if truncated else []
    updated_at = int(existing.get("updated_at") or _now()) if existing.get("content_digest") == digest else _now()
    pack: dict[str, Any] = {
        "version": version,
        "updated_at": updated_at,
        "root_goal": str(root_goal or ""),
        "request": str(request or ""),
        "discord_thread_context": str(thread_context or ""),
        "message_count": _context_pack_message_count(thread_context),
        "truncated": truncated,
        "source_message_ids": _context_pack_source_message_ids("\n".join([root_goal, request, thread_context])),
        "plan_artifacts": normalized_artifacts,
        "discord_context_quality": context_quality,
        "warnings": warnings,
        "content_digest": digest,
    }
    pack["markdown"] = render_context_pack_markdown(pack)
    json_path = context_pack_path(board)
    md_path = context_pack_markdown_path(board)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json_write(json_path, pack, indent=2)
    md_path.write_text(pack["markdown"], encoding="utf-8")
    return pack


def _write_metadata(board: str, metadata: dict[str, Any]) -> dict[str, Any]:
    path = _metadata_path(board)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(metadata)
    payload.pop("db_path", None)
    atomic_json_write(path, payload, indent=2)
    payload["db_path"] = str(kanban_db.kanban_db_path(board))
    return payload


def _read_metadata_from_path(board: str, path: Path | None = None) -> dict[str, Any]:
    if path is not None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return raw
        except Exception:
            logger.debug("Failed to read Discord worker metadata from %s", path, exc_info=True)
    return kanban_db.read_board_metadata(board)


def _write_metadata_to_path(board: str, metadata: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    if path is None:
        return _write_metadata(board, metadata)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(metadata)
    payload.pop("db_path", None)
    atomic_json_write(path, payload, indent=2)
    payload["db_path"] = str(kanban_db.kanban_db_path(board))
    return payload


def _mutate_worker_metadata(
    board: str,
    mutator: Callable[[dict[str, Any], dict[str, Any]], bool | None],
    *,
    metadata_path: Path | None = None,
    warning_action: str = "update Discord worker metadata",
) -> dict[str, Any] | None:
    path = metadata_path or _metadata_path(board)
    try:
        with _board_metadata_lock(path):
            metadata = _read_metadata_from_path(board, metadata_path)
            worker = dict(metadata.get(DISCORD_WORKER_META_KEY) or {})
            changed = mutator(metadata, worker)
            if not changed:
                metadata[DISCORD_WORKER_META_KEY] = worker
                return metadata
            metadata[DISCORD_WORKER_META_KEY] = worker
            return _write_metadata_to_path(board, metadata, metadata_path)
    except BoardMetadataLockTimeout:
        logger.warning(
            "Timed out acquiring Discord worker metadata lock for board %s while trying to %s; skipping stale write",
            board,
            warning_action,
        )
        return None


def dispatch_dirty_marker_path() -> Path:
    """Return the cross-process marker used to wake gateway dispatch."""
    return kanban_db.kanban_home() / "kanban" / DISCORD_WORKER_DISPATCH_DIRTY_FILENAME


def mark_dispatch_dirty(*, board: Optional[str] = None, reason: str = "") -> Path:
    """Signal that Discord worker dispatch should run soon.

    Gateway-created work can use an in-process event, but role workers are
    subprocesses. This tiny marker gives those subprocesses a safe way to wake
    the embedded dispatcher without sharing an event loop.
    """
    path = dispatch_dirty_marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json_write(
        path,
        {
            "updated_at": _now(),
            "board": str(board or ""),
            "reason": str(reason or ""),
        },
        indent=2,
    )
    return path


def dispatch_dirty_marker_mtime_ns() -> int:
    try:
        return dispatch_dirty_marker_path().stat().st_mtime_ns
    except OSError:
        return 0


def _read_worker_meta(board: str) -> dict[str, Any]:
    metadata = kanban_db.read_board_metadata(board)
    raw = metadata.get(DISCORD_WORKER_META_KEY)
    return dict(raw) if isinstance(raw, dict) else {}


def _worker_source_message_id(worker: dict[str, Any]) -> str:
    return str(
        worker.get("source_message_id")
        or worker.get("request_id")
        or worker.get("thread_id")
        or ""
    ).strip()


def _worker_source_message_too_old(worker: dict[str, Any]) -> bool:
    return discord_message_exceeds_age_limit(_worker_source_message_id(worker))


def _clear_stale_terminal_sync_flags(board: str, worker: dict[str, Any]) -> None:
    if not (
        worker.get("terminal_reaction_sync_pending")
        or worker.get("terminal_summary_sync_pending")
        or worker.get("terminal_completion_message_pending")
    ):
        return
    mark_thread_status_synced(
        board,
        reaction=True,
        summary=True,
        completion_message=True,
    )


_TERMINAL_SUMMARY_SYNC_FIELDS = frozenset(
    {
        "goal_status",
        "phase",
        "concise_outcome",
        "deployment_status",
        "final_discord_response",
        "final_discord_response_at",
        "final_discord_session_id",
        "final_discord_work_item_id",
        "final_discord_message_id",
        "blocked_reason",
        "pr_url",
        "pr_number",
        "pr_error",
        "pr_status_error",
        "pr_state",
        "pr_merge_state",
        "pr_mergeable",
        "pr_is_draft",
        "pr_review_decision",
        "pr_merged_at",
        "pr_merge_commit",
        "pr_checks_status",
        "pr_checks_total",
        "pr_checks_failed",
        "pr_ci_wait_state",
        "pr_ci_wait_started_at",
        "pr_ci_next_poll_at",
        "pr_ci_wait_seconds",
        "pr_ci_head_sha",
        "pr_blocker",
    }
)


def _terminal_worker_reaction_state(worker: dict[str, Any]) -> str:
    status = str(worker.get("goal_status") or "").strip().lower()
    phase = str(worker.get("phase") or "").strip().lower()
    if worker.get("cancelled") or status == "cancelled":
        return "errored"
    if _pr_finalizer_failure_is_pending_checks(worker):
        return "running"
    if _terminal_worker_has_non_green_finalization(worker):
        return "blocked"
    if status == "blocked" and phase != "complete":
        return "blocked"
    if status == "done" or phase == "complete":
        return "done"
    return ""


def _terminal_worker_has_non_green_finalization(worker: dict[str, Any]) -> bool:
    pr_success = _worker_has_successful_pr_terminal_evidence(worker)
    if not pr_success:
        if any(str(worker.get(key) or "").strip() for key in ("pr_blocker", "pr_error", "pr_status_error")):
            return True
    if [item for item in worker.get("pr_checks_failed") or [] if str(item).strip()]:
        return True
    raw_checks_status = worker.get("pr_checks_status")
    checks_status = str(raw_checks_status or "").strip().lower()
    if checks_status in {"failed", "failure", "error", "errored", "cancelled", "timed out", "timed_out"}:
        return True
    return False


def _worker_meta_allows_ready_dispatch(worker: dict[str, Any], *, counts: dict[str, Any]) -> bool:
    """Return True when live Kanban work supersedes stale finalizer-blocked metadata."""
    status = str(worker.get("goal_status") or "").strip().lower()
    phase = str(worker.get("phase") or "").strip().lower()
    if status != "blocked" and phase != "blocked":
        return False
    if not (int(counts.get("ready") or 0) > 0 or int(counts.get("review") or 0) > 0):
        return False
    blocked_reason = str(worker.get("blocked_reason") or "").strip().lower()
    finalizer_blocker = str(worker.get("pr_blocker") or worker.get("pr_error") or "").strip()
    return (
        blocked_reason == "approved reviewer pr finalization failed"
        or bool(finalizer_blocker)
        or _pr_finalizer_failure_is_failed_checks(worker)
        or _pr_finalizer_failure_is_merge_conflict(worker)
    )


def _has_dispatchable_worker_tasks(conn: Any) -> bool:
    placeholders = ",".join("?" for _ in ROLE_ASSIGNEES)
    if not placeholders:
        return False
    row = conn.execute(
        "SELECT COUNT(*) FROM tasks "
        "WHERE status IN ('ready', 'review') "
        "AND lower(assignee) IN "
        f"({placeholders})",
        tuple(sorted(ROLE_ASSIGNEES)),
    ).fetchone()
    return bool(row and int(row[0] or 0) > 0)


def _terminal_reaction_synced_state(worker: dict[str, Any]) -> str:
    return str(worker.get("terminal_reaction_synced_state") or "").strip().lower()


def _is_terminal_worker_meta(worker: dict[str, Any]) -> bool:
    status = str(worker.get("goal_status") or "").strip().lower()
    phase = str(worker.get("phase") or "").strip().lower()
    return bool(worker.get("cancelled") or status in TERMINAL_GOAL_STATUSES or phase == "complete")


def _terminal_worker_reaction_sync_needed(worker: dict[str, Any]) -> bool:
    reaction_state = _terminal_worker_reaction_state(worker)
    if reaction_state not in {"done", "blocked", "errored"}:
        return False
    if worker.get("terminal_reaction_sync_pending"):
        return True
    return _terminal_reaction_synced_state(worker) != reaction_state


def _terminal_worker_status_sync_needed(worker: dict[str, Any]) -> bool:
    if worker.get("kind") != "discord_worker_board" or not _is_terminal_worker_meta(worker):
        return False
    return bool(
        worker.get("terminal_summary_sync_pending")
        or worker.get("terminal_completion_message_pending")
        or _terminal_worker_reaction_sync_needed(worker)
    )

def mark_completion_notice_pending_on_done_transition(
    worker: dict[str, Any],
    previous: dict[str, Any] | None = None,
) -> bool:
    """Set the one-shot Discord completion notice flag when a board becomes done.

    This complements the codex worker's direct phase update path. Foreman and
    read-token updates mutate board metadata through helper modules, so the
    completion notice has to be armed at the metadata layer too.
    """

    if worker.get("kind") != "discord_worker_board":
        return False
    previous = previous or {}
    if _terminal_worker_reaction_state(previous) == "done":
        return False
    if _terminal_worker_reaction_state(worker) != "done":
        return False
    return _arm_terminal_completion_notice_if_ready(worker)


def board_has_unsynced_terminal_reaction(board: str) -> bool:
    """Return whether a terminal Discord worker board still needs reaction sync."""
    worker = _read_worker_meta(board)
    if worker.get("kind") != "discord_worker_board" or not _is_terminal_worker_meta(worker):
        return False
    try:
        reaction_state = board_thread_reaction_state(board)
    except Exception:
        reaction_state = ""
    if reaction_state not in {"done", "blocked", "errored"}:
        return False
    if worker.get("terminal_reaction_sync_pending"):
        return True
    return _terminal_reaction_synced_state(worker) != reaction_state


def board_has_pending_terminal_completion_notice(board: str) -> bool:
    """Return whether a done Discord worker board still needs its follow-up post."""
    worker = _read_worker_meta(board)
    if worker.get("kind") != "discord_worker_board" or _terminal_worker_reaction_state(worker) != "done":
        return False
    return bool(worker.get("terminal_completion_message_pending"))


def _board_has_completion_notice_proof(worker: dict[str, Any]) -> bool:
    return bool(worker.get("terminal_completion_message_sent_at") or worker.get("terminal_completion_message_id"))


def _arm_terminal_completion_notice_if_ready(worker: dict[str, Any]) -> bool:
    if worker.get("kind") != "discord_worker_board":
        return False
    if _terminal_worker_reaction_state(worker) != "done":
        return False
    if _board_has_completion_notice_proof(worker):
        return False
    if worker.get("terminal_completion_message_pending") is True:
        return False
    worker["terminal_completion_message_pending"] = True
    return True


def _update_worker_meta(board: str, updates: dict[str, Any]) -> dict[str, Any]:
    terminal_summary_changed = False

    def mutate(metadata: dict[str, Any], worker: dict[str, Any]) -> bool:
        nonlocal terminal_summary_changed
        previous = dict(worker)
        for key, value in updates.items():
            if value is _DELETE_META:
                worker.pop(key, None)
            else:
                worker[key] = value
        changed_keys = {key for key in set(previous) | set(worker) if previous.get(key) != worker.get(key)}
        if not changed_keys:
            return False
        terminal_summary_changed = bool(changed_keys & _TERMINAL_SUMMARY_SYNC_FIELDS)
        became_terminal = not _is_terminal_worker_meta(previous) and _is_terminal_worker_meta(worker)
        terminal_reaction_changed = _terminal_worker_reaction_state(previous) != _terminal_worker_reaction_state(worker)
        if worker.get("kind") == "discord_worker_board" and _is_terminal_worker_meta(worker):
            if terminal_summary_changed:
                worker["terminal_summary_sync_pending"] = True
            if became_terminal or terminal_reaction_changed:
                worker["terminal_reaction_sync_pending"] = True
            mark_completion_notice_pending_on_done_transition(worker, previous)
        worker["updated_at"] = _now()
        return True

    written = _mutate_worker_metadata(board, mutate)
    if written is None:
        return kanban_db.read_board_metadata(board)
    worker = dict(written.get(DISCORD_WORKER_META_KEY) or {})
    if worker.get("kind") == "discord_worker_board" and _is_terminal_worker_meta(worker) and terminal_summary_changed:
        try:
            persist_board_run_summary(board)
        except Exception:
            logger.debug("Failed to refresh Discord board run summary for %s", board, exc_info=True)
        try:
            mark_dispatch_dirty(board=board, reason="terminal-summary-metadata-updated")
        except Exception:
            logger.debug("Failed to mark Discord worker dispatch dirty for %s", board, exc_info=True)
        written = kanban_db.read_board_metadata(board)
    return written


def _clear_terminal_summary_fields(worker: dict[str, Any]) -> None:
    for key in (
        "board_summary",
        "board_summary_path",
        "board_summary_updated_at",
        "terminal_completion_message_pending",
        "terminal_completion_message_sent_at",
        "terminal_completion_message_id",
        "terminal_summary_message_sent_at",
        "terminal_summary_sync_pending",
        "terminal_reaction_sync_pending",
        "terminal_reaction_synced_state",
    ):
        worker[key] = _DELETE_META


def _clear_board_run_summary(board: str, worker: dict[str, Any]) -> None:
    _clear_terminal_summary_fields(worker)
    try:
        board_run_summary_path(board).unlink(missing_ok=True)
    except OSError:
        pass


def _clear_pr_summary_fields(worker: dict[str, Any]) -> None:
    for key in (
        "pr_url",
        "pr_number",
        "pr_error",
        "pr_status_error",
        "pr_state",
        "pr_merge_state",
        "pr_mergeable",
        "pr_is_draft",
        "pr_review_decision",
        "pr_merged_at",
        "pr_merge_commit",
        "pr_checks_status",
        "pr_checks_total",
        "pr_checks_failed",
        "pr_ci_wait_state",
        "pr_ci_wait_started_at",
        "pr_ci_next_poll_at",
        "pr_ci_wait_seconds",
        "pr_ci_head_sha",
        "pr_blocker",
    ):
        worker[key] = _DELETE_META


def _clear_generated_summary_title(worker: dict[str, Any]) -> None:
    worker["summary_title"] = _DELETE_META


def _reset_pr_amend_worker_for_new_source(worker: dict[str, Any]) -> None:
    """Clear terminal/finalizer state before reusing a board for a new review."""

    worker["phase"] = "intake"
    worker["goal_status"] = "unset"
    worker["execution_mode"] = "pending"
    worker["criteria"] = []
    for key in (
        "blocked_reason",
        "concise_outcome",
        "deployment_status",
        "final_discord_response",
        "final_discord_response_at",
        "final_discord_session_id",
        "final_discord_work_item_id",
        "final_discord_message_id",
        "pr_amend_trigger_head_sha",
        "pr_amend_upstream_head_sha",
        "pr_amend_head_advanced",
        "github_pr_amend_head_sha",
        "pr_finalizer_recovery_state",
        "pr_finalizer_recovery_blocker",
        "pr_skipped_no_changes",
        "pr_merge_skipped",
        "pr_merge_skipped_reason",
    ):
        worker[key] = _DELETE_META
    _clear_terminal_summary_fields(worker)
    _clear_pr_summary_fields(worker)
    _clear_generated_summary_title(worker)


def _public_base_url() -> str:
    value = str(os.getenv("HERMES_PUBLIC_KANBAN_BASE_URL") or "").strip()
    if value:
        return value.rstrip("/")
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
        value = ((cfg.get("kanban") or {}).get("discord_worker") or {}).get("public_base_url") or ""
        if value:
            return str(value).strip().rstrip("/")
    except Exception:
        pass
    return ""


def _absolute_public_base_url() -> str:
    base = _public_base_url()
    if base.startswith(("http://", "https://")):
        return base
    return ""


def _public_workers_base_url() -> str:
    base = _absolute_public_base_url()
    if not base:
        return ""
    parts = urlsplit(base)
    path = parts.path.rstrip("/")
    if path.endswith("/kanban"):
        path = path[: -len("/kanban")]
    if not path.endswith("/workers"):
        path = f"{path}/workers"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def public_session_board_url(session_id: str) -> str:
    base = _public_workers_base_url()
    session = quote(str(session_id or "").strip(), safe="")
    return f"{base}/{session}" if base and session else ""


def _starter_message_thread_id_from_slug(session_id: str) -> str:
    raw = str(session_id or "").strip()
    if not raw.startswith("discord-") or "-m-" not in raw:
        return ""
    thread_part, request_part = raw[len("discord-") :].rsplit("-m-", 1)
    return thread_part if thread_part and thread_part == request_part else ""


def _public_session_id_for_board(board: str, worker: dict[str, Any]) -> str:
    thread_id = str(worker.get("thread_id") or "").strip()
    if not thread_id:
        return str(board or "")
    try:
        legacy = board_slug_for_discord_thread(thread_id)
    except ValueError:
        legacy = ""
    request_id = str(
        worker.get("request_id") or worker.get("source_message_id") or ""
    ).strip()
    starter = ""
    if request_id and request_id == thread_id:
        try:
            starter = board_slug_for_discord_request(thread_id, request_id)
        except ValueError:
            starter = ""
    return thread_id if board in {legacy, starter} else str(board or thread_id)


def resolve_public_session_board(session_id: str) -> str:
    raw = str(session_id or "").strip()
    if not raw:
        raise KeyError("unknown board session")
    candidates = [raw]
    starter_thread_id = _starter_message_thread_id_from_slug(raw)
    if starter_thread_id:
        candidates.append(board_slug_for_discord_thread(starter_thread_id))
    elif not raw.startswith("discord-"):
        candidates.append(board_slug_for_discord_thread(raw))
        candidates.append(board_slug_for_discord_request(raw, raw))
    for board in candidates:
        if not kanban_db.board_exists(board):
            continue
        worker = _read_worker_meta(board)
        if worker.get("kind") == "discord_worker_board":
            return board
    raise KeyError("unknown board session")


def public_board_url(token: str) -> str:
    """Return the token URL when an absolute public base is configured."""
    path = f"/public/kanban/{token}"
    base = _public_workers_base_url()
    return f"{base}{path}" if base else ""


def _discord_thread_url(worker: dict[str, Any]) -> str:
    guild_id = str(worker.get("guild_id") or "").strip()
    thread_id = str(worker.get("thread_id") or "").strip()
    if not guild_id or not thread_id:
        return ""
    return (
        "https://discord.com/channels/"
        f"{quote(guild_id, safe='')}/{quote(thread_id, safe='')}"
    )


def _default_worktree_path(project_path: Optional[str], thread_id: str) -> str:
    repo_name = Path(project_path or os.getcwd()).resolve().name or "project"
    safe_repo = re.sub(r"[^a-zA-Z0-9_.-]+", "-", repo_name).strip("-") or "project"
    return str(Path("/home/droid/workspaces") / f"{safe_repo}-discord-{thread_id}")


@dataclass
class DiscordBoard:
    slug: str
    metadata: dict[str, Any]

    @property
    def worker(self) -> dict[str, Any]:
        raw = self.metadata.get(DISCORD_WORKER_META_KEY)
        return dict(raw) if isinstance(raw, dict) else {}

    @property
    def public_url(self) -> str:
        return str(self.worker.get("public_url") or "")


def _bounded_project_inspection_candidates(value: Any) -> list[dict[str, str]]:
    """Revalidate the structured candidate list at the board boundary."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if isinstance(value, (list, tuple)):
        value = [
            item
            if isinstance(item, dict)
            else {
                "url": getattr(item, "url", ""),
                "environment": getattr(item, "environment", ""),
                "location": getattr(item, "location", ""),
            }
            for item in value
        ]
    try:
        from hermes_cli.project_inspection import (
            normalize_project_inspection_candidates,
            project_inspection_candidates_to_dicts,
        )

        return project_inspection_candidates_to_dicts(
            normalize_project_inspection_candidates(value)
        )
    except Exception:
        pass
    if not isinstance(value, (list, tuple)):
        return []
    candidates: list[dict[str, str]] = []
    for item in value[:12]:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        environment = str(item.get("environment") or "").strip().lower()
        location = str(item.get("location") or "").strip().lower()
        try:
            parsed = urlsplit(url)
        except ValueError:
            continue
        if (
            len(url) > 2048
            or parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or any(ord(char) < 32 or ord(char) == 127 for char in url)
            or environment not in {"development", "production"}
            or location not in {"local", "external"}
        ):
            continue
        candidates.append(
            {
                "url": url,
                "environment": environment,
                "location": location,
            }
        )
    return candidates


def project_inspection_prompt_for_context(context: Any) -> str:
    """Render the shared ordered, navigation-only fallback contract."""
    raw = context if isinstance(context, dict) else {}
    candidates = _bounded_project_inspection_candidates(
        raw.get("project_inspection_candidates")
    )
    if not candidates:
        return ""
    lines = ["Project inspection contract (ordered, development first):"]
    for index, candidate in enumerate(candidates, start=1):
        lines.append(
            f"{index}. {candidate['url']} "
            f"({candidate['location']} {candidate['environment']})"
        )
    lines.extend(
        [
            "- Try the first development candidate, then use the next candidate only "
            "when connection, DNS, or navigation is unavailable.",
            "- Once navigation succeeds, inspect that origin. Do not switch to production "
            "because of login, application content, an error state, or a failed assertion.",
            "- If no configured candidate is reachable, start a repository-local preview "
            "server and report the exact preview URL used.",
        ]
    )
    return "\n".join(lines)


def _normalized_visual_requirement(value: Any) -> dict[str, Any]:
    try:
        from agent.visual_qa import normalize_visual_requirement

        return normalize_visual_requirement(value)
    except Exception:
        return {"level": "none", "target": "", "assertions": []}


def ensure_discord_thread_board(
    *,
    thread_id: str,
    chat_id: Optional[str] = None,
    guild_id: Optional[str] = None,
    parent_channel_id: Optional[str] = None,
    initial_request: str = "",
    project_context: Optional[dict[str, Any]] = None,
    request_id: Optional[str] = None,
    board_slug: Optional[str] = None,
    source_message_id: Optional[str] = None,
    summary_message_id: Optional[str] = None,
) -> DiscordBoard:
    """Create or update the board backing a Discord thread/request."""
    started = time.time()
    thread_id = str(thread_id or "").strip()
    request_id = str(source_message_id or request_id or "").strip()
    legacy_slug = board_slug_for_discord_thread(thread_id)
    starter_slug = ""
    is_starter_message = bool(request_id and request_id == thread_id)
    if is_starter_message:
        starter_slug = board_slug_for_discord_request(thread_id, request_id)
    default_slug = board_slug_for_discord_request(
        thread_id,
        "" if is_starter_message else request_id,
    )
    slug = str(board_slug or "").strip()
    if not slug:
        slug = starter_slug if starter_slug and kanban_db.board_exists(starter_slug) else default_slug
    route_id = str(thread_id) if slug in {legacy_slug, starter_slug} else slug
    worker = _read_worker_meta(slug)
    existing_context = worker.get("project_context")
    merged_project_context = dict(existing_context) if isinstance(existing_context, dict) else {}
    incoming_project_context = dict(project_context or {})
    for key, value in incoming_project_context.items():
        if value is None:
            continue
        if key == "project_inspection_candidates":
            normalized_candidates = _bounded_project_inspection_candidates(value)
            if normalized_candidates:
                merged_project_context[key] = normalized_candidates
            elif key not in merged_project_context:
                merged_project_context[key] = []
            continue
        merged_project_context[key] = value
    incoming_project_path = str(incoming_project_context.get("project_path") or "").strip() or None
    existing_project_path = str(worker.get("project_path") or "").strip() or None
    project_path = incoming_project_path or existing_project_path
    if project_path:
        merged_project_context["project_path"] = project_path
    existing_pr_amend_context = (
        existing_context.get("github_pr_amend") if isinstance(existing_context, dict) else None
    )
    incoming_pr_amend_context = incoming_project_context.get("github_pr_amend")
    existing_pr_amend_source_key = ""
    incoming_pr_amend_source_key = ""
    if isinstance(existing_pr_amend_context, dict):
        existing_pr_amend_source_key = str(existing_pr_amend_context.get("source_key") or "").strip()
    if isinstance(incoming_pr_amend_context, dict):
        incoming_pr_amend_source_key = str(incoming_pr_amend_context.get("source_key") or "").strip()
    reset_review_loop_budget = bool(
        incoming_pr_amend_source_key
        and incoming_pr_amend_source_key != existing_pr_amend_source_key
    )
    token = worker.get("share_token") or secrets.token_urlsafe(PUBLIC_TOKEN_BYTES)
    request_suffix = slug.removeprefix("discord-")
    branch = f"discord/{request_suffix or thread_id}"
    metadata = kanban_db.create_board(
        slug,
        name=f"Discord {thread_id}" if route_id == str(thread_id) else f"Discord {thread_id} / {request_id}",
        description=(initial_request or "")[:500],
        icon="",
        color="#22c55e",
    )
    previous_worktree_path = str(worker.get("worktree_path") or "").strip()
    project_path_changed = bool(project_path and project_path != existing_project_path)
    worktree_path = previous_worktree_path
    if not worktree_path or (project_path_changed and not worker.get("code_island_ready")):
        worktree_path = _default_worktree_path(project_path, request_suffix or str(thread_id))
    request_text = str(initial_request or worker.get("initial_request") or "")
    pr_policy = pr_policy_for_request(request_text)
    worker.update(
        {
            "kind": "discord_worker_board",
            "thread_id": str(thread_id),
            "request_id": request_id or worker.get("request_id") or "",
            "source_message_id": str(source_message_id or worker.get("source_message_id") or request_id or ""),
            "summary_message_id": str(summary_message_id or worker.get("summary_message_id") or ""),
            "chat_id": str(chat_id or ""),
            "guild_id": str(guild_id or ""),
            "parent_channel_id": str(parent_channel_id or ""),
            "initial_request": request_text,
            "project_context": merged_project_context,
            "project_path": project_path,
            "base_branch": worker.get("base_branch") or merged_project_context.get("base_branch") or "main",
            "worker_branch": worker.get("worker_branch") or branch,
            "worktree_path": worktree_path,
            "execution_mode": worker.get("execution_mode") or "pending",
            "phase": worker.get("phase") or "intake",
            "goal_status": worker.get("goal_status") or "unset",
            "criteria": worker.get("criteria") or [],
            "review_loop_count": 0 if reset_review_loop_budget else int(worker.get("review_loop_count") or 0),
            "review_loop_limit": (
                _review_loop_limit_for_request(request_text)
                if reset_review_loop_budget
                else int(worker.get("review_loop_limit") or _review_loop_limit_for_request(request_text))
            ),
            "pr_open_policy": pr_policy["pr_open_policy"],
            "merge_policy": pr_policy["merge_policy"],
            "share_token": token,
            "public_url": public_session_board_url(route_id),
            "created_at": worker.get("created_at") or _now(),
        }
    )
    if reset_review_loop_budget:
        _reset_pr_amend_worker_for_new_source(worker)
    _mark_code_island_deferred(worker)
    setup_updates = {
        key: value
        for key, value in worker.items()
        if key
        and (value is _DELETE_META or key not in {
            "terminal_reaction_sync_pending",
            "terminal_summary_sync_pending",
            "terminal_completion_message_pending",
            "terminal_completion_message_sent_at",
            "terminal_completion_message_id",
            "terminal_reaction_synced_state",
        })
    }

    def apply_setup_updates(current_metadata: dict[str, Any], current_worker: dict[str, Any]) -> bool:
        for key, value in setup_updates.items():
            if value is _DELETE_META:
                current_worker.pop(key, None)
            else:
                current_worker[key] = value
        return True

    metadata = _mutate_worker_metadata(
        slug,
        apply_setup_updates,
        warning_action="ensure Discord worker board metadata",
    ) or metadata
    if previous_worktree_path != str(worker.get("worktree_path") or ""):
        _sync_role_task_workspaces(
            slug,
            old_path=previous_worktree_path,
            new_path=str(worker.get("worktree_path") or ""),
        )
    elapsed_ms = int((time.time() - started) * 1000)
    logger.info(
        "discord_worker_board_setup board=%s deferred_code_island=%s ready=%s total_ms=%d",
        slug,
        bool(worker.get("code_island_pending")),
        bool(worker.get("code_island_ready")),
        elapsed_ms,
    )
    return DiscordBoard(slug=slug, metadata=metadata)


def set_feature_summary_handle(
    board: str,
    *,
    message_id: Optional[str] = None,
    source_message_id: Optional[str] = None,
) -> None:
    """Attach the Discord summary/source message ids to a worker board."""
    updates: dict[str, Any] = {}
    if message_id:
        updates["summary_message_id"] = str(message_id)
    if source_message_id:
        updates["source_message_id"] = str(source_message_id)
        updates["request_id"] = str(source_message_id)
    if updates:
        _update_worker_meta(board, updates)


def _sync_role_task_workspaces(board: str, *, old_path: str, new_path: str) -> None:
    """Move queued role-lane tasks when board project context is repaired."""
    if not new_path:
        return
    statuses = ("triage", "todo", "ready", "blocked", "review")
    assignees = tuple(sorted(ROLE_ASSIGNEES))
    placeholders = ",".join("?" for _ in assignees)
    status_placeholders = ",".join("?" for _ in statuses)
    params: list[Any] = [new_path, *assignees, *statuses]
    path_clause = "workspace_path = ?"
    if old_path:
        params.append(old_path)
    else:
        path_clause = "(workspace_path IS NULL OR workspace_path = '')"
    conn = kanban_db.connect(board=board)
    try:
        conn.execute(
            "UPDATE tasks SET workspace_path = ? "
            "WHERE workspace_kind = 'dir' "
            f"AND lower(assignee) IN ({placeholders}) "
            f"AND status IN ({status_placeholders}) "
            f"AND {path_clause}",
            tuple(params),
        )
        conn.commit()
    finally:
        conn.close()


def _code_island_configured(worker: dict[str, Any]) -> bool:
    project_path = str(worker.get("project_path") or "").strip()
    worktree_path = str(worker.get("worktree_path") or "").strip()
    branch = str(worker.get("worker_branch") or "").strip()
    return bool(project_path and worktree_path and branch and os.path.isdir(project_path))


def _is_git_worktree(path: str) -> bool:
    if not path or not os.path.isdir(path):
        return False
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return False
    return result.returncode == 0 and (result.stdout or "").strip().lower() == "true"


def _git_text(cwd: str, args: list[str], *, timeout: int = 10) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return (result.stdout or "").strip()


def _current_worktree_branch(worktree_path: str) -> str:
    return _git_text(worktree_path, ["branch", "--show-current"]) or ""


def _quarantine_generated_plan_artifacts(worktree_path: str, worker: dict[str, Any]) -> None:
    plans_dir = Path(worktree_path) / ".hermes" / "plans"
    if not plans_dir.exists():
        return
    try:
        if plans_dir.is_dir() and not any(plans_dir.iterdir()):
            plans_dir.rmdir()
            return
    except OSError:
        return
    try:
        from hermes_constants import get_hermes_home

        safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "-", Path(worktree_path).name).strip("-") or "worktree"
        dest = get_hermes_home() / "gateway" / "discord-plan-quarantine" / safe_name / str(_now())
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(plans_dir), str(dest))
        worker["generated_plan_quarantine_path"] = str(dest)
        worker["generated_plan_quarantined_at"] = _now()
        try:
            (Path(worktree_path) / ".hermes").rmdir()
        except OSError:
            pass
    except Exception as exc:
        worker["generated_plan_quarantine_error"] = str(exc)


def _is_generated_plan_status_line(line: str) -> bool:
    path = line[3:].strip() if len(line) > 3 else line.strip()
    return path == ".hermes/plans" or path.startswith(".hermes/plans/")


def _meaningful_worktree_status(worktree_path: str) -> list[str]:
    status = _git_text(
        worktree_path,
        ["status", "--porcelain", "--untracked-files=all"],
    )
    if not status:
        return []
    return [line for line in status.splitlines() if not _is_generated_plan_status_line(line)]


def _worktree_head_merged_into(worktree_path: str, base_branch: str) -> bool:
    refs = [base_branch]
    if base_branch and not base_branch.startswith("origin/"):
        refs.append(f"origin/{base_branch}")
    for ref in [r for r in refs if r]:
        try:
            verify = subprocess.run(
                ["git", "rev-parse", "--verify", "--quiet", ref],
                cwd=worktree_path,
                timeout=10,
            )
            if verify.returncode != 0:
                continue
            merged = subprocess.run(
                ["git", "merge-base", "--is-ancestor", "HEAD", ref],
                cwd=worktree_path,
                timeout=20,
            )
        except Exception:
            continue
        if merged.returncode == 0:
            return True
    return False


def _worktree_base_start_ref(repo_root: str, base_branch: str) -> str:
    base_branch = str(base_branch or "").strip() or "main"
    refs = [base_branch]
    if not base_branch.startswith("origin/"):
        refs.append(f"origin/{base_branch}")
    for ref in refs:
        try:
            verify = subprocess.run(
                ["git", "rev-parse", "--verify", "--quiet", ref],
                cwd=repo_root,
                timeout=10,
            )
        except Exception:
            continue
        if verify.returncode == 0:
            return ref
    return base_branch


def _remove_clean_merged_worktree(repo_root: str, worktree_path: str) -> Optional[str]:
    cwd = repo_root if repo_root and os.path.isdir(repo_root) else worktree_path
    try:
        result = subprocess.run(
            ["git", "worktree", "remove", worktree_path],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except Exception as exc:
        return str(exc)
    if result.returncode == 0:
        return None
    return (result.stderr or result.stdout or "git worktree remove failed").strip()


def _prepare_existing_code_island_worktree(
    worker: dict[str, Any],
    *,
    repo_root: str,
    branch: str,
    base_branch: str,
) -> bool:
    """Return True when the existing worktree path has been fully handled."""
    worktree_path = str(worker.get("worktree_path") or "").strip()
    if not os.path.isdir(worktree_path):
        return False
    if not _is_git_worktree(worktree_path):
        worker["code_island_ready"] = False
        worker["code_island_pending"] = False
        worker["code_island_error"] = f"worktree path is not a git repository: {worktree_path}"
        return True

    _quarantine_generated_plan_artifacts(worktree_path, worker)
    current_branch = _current_worktree_branch(worktree_path)
    if branch and current_branch and current_branch != branch:
        status = _meaningful_worktree_status(worktree_path)
        if status:
            worker["code_island_ready"] = False
            worker["code_island_pending"] = False
            worker["code_island_error"] = (
                f"worker checkout is on {current_branch!r}, expected {branch!r}, "
                f"and has local changes: {'; '.join(status[:5])}"
            )
            return True
        if _worktree_head_merged_into(worktree_path, base_branch):
            removed_error = _remove_clean_merged_worktree(repo_root, worktree_path)
            if removed_error is None:
                worker["stale_worktree_removed_at"] = _now()
                worker["stale_worktree_previous_branch"] = current_branch
                worker["code_island_ready"] = False
                worker["code_island_pending"] = True
                worker.pop("code_island_error", None)
                return False
            worker["code_island_ready"] = False
            worker["code_island_pending"] = False
            worker["code_island_error"] = f"could not remove stale worker checkout: {removed_error}"
            return True
        worker["code_island_ready"] = False
        worker["code_island_pending"] = False
        worker["code_island_error"] = (
            f"worker checkout is on {current_branch!r}, expected {branch!r}; "
            f"not removing because HEAD is not merged into {base_branch!r}"
        )
        return True

    worker["code_island_ready"] = True
    worker["code_island_pending"] = False
    worker.pop("code_island_error", None)
    return True


def _code_island_blocker(worker: dict[str, Any]) -> str:
    project_path = str(worker.get("project_path") or "").strip()
    worktree_path = str(worker.get("worktree_path") or "").strip()
    branch = str(worker.get("worker_branch") or "").strip()
    context = worker.get("project_context") if isinstance(worker.get("project_context"), dict) else {}
    channel = str(context.get("project_channel_id") or worker.get("parent_channel_id") or "").strip()
    if not project_path:
        suffix = f" for Discord channel {channel}" if channel else ""
        return f"No project checkout is mapped{suffix}; configure discord.channel_cwds before running workers."
    if not os.path.isdir(project_path):
        return f"Mapped project checkout does not exist: {project_path}"
    if not branch:
        return "Discord worker branch is not configured."
    if not worktree_path:
        return "Discord worker worktree path is not configured."
    if os.path.isdir(worktree_path) and not _is_git_worktree(worktree_path):
        return f"Worker checkout exists but is not a git repository: {worktree_path}"
    error = str(worker.get("code_island_error") or "").strip()
    if error:
        return f"Could not prepare worker checkout: {error}"
    return ""


def _active_code_island_health_error(worker: dict[str, Any], *, active_pipeline: bool) -> bool:
    if not active_pipeline:
        return False
    return bool(_code_island_blocker(worker))


def _code_island_telemetry_state(worker: dict[str, Any]) -> tuple[bool, bool, str, str]:
    return (
        bool(worker.get("code_island_ready")),
        bool(worker.get("code_island_pending")),
        str(worker.get("code_island_error") or ""),
        str(worker.get("worktree_path") or ""),
    )


def _block_worker_board_for_code_island(board: str, worker: dict[str, Any], reason: str) -> None:
    worker.update(
        {
            "phase": "blocked",
            "goal_status": "blocked",
            "blocked_reason": reason,
            "code_island_pending": False,
            "terminal_reaction_sync_pending": True,
            "terminal_summary_sync_pending": True,
            "updated_at": _now(),
        }
    )
    _update_worker_meta(board, worker)
    persist_board_run_summary(board)


def _mark_code_island_deferred(worker: dict[str, Any]) -> None:
    if not _code_island_configured(worker):
        worker["code_island_pending"] = False
        return
    worktree_path = str(worker.get("worktree_path") or "").strip()
    if os.path.isdir(worktree_path):
        if _is_git_worktree(worktree_path):
            worker["code_island_ready"] = True
            worker["code_island_pending"] = False
            worker.pop("code_island_error", None)
        else:
            worker["code_island_ready"] = False
            worker["code_island_pending"] = True
            worker["code_island_error"] = f"worktree path is not a git repository: {worktree_path}"
        return
    worker["code_island_ready"] = False
    worker["code_island_pending"] = True
    worker["code_island_requested_at"] = worker.get("code_island_requested_at") or _now()


def _ensure_code_island(worker: dict[str, Any]) -> None:
    project_path = str(worker.get("project_path") or "").strip()
    worktree_path = str(worker.get("worktree_path") or "").strip()
    branch = str(worker.get("worker_branch") or "").strip()
    base_branch = str(worker.get("base_branch") or "main").strip() or "main"
    if not project_path or not worktree_path or not branch or not os.path.isdir(project_path):
        worker["code_island_pending"] = False
        return

    try:
        root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=project_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:
        worker["code_island_ready"] = False
        worker["code_island_pending"] = True
        worker["code_island_error"] = str(exc)
        return
    if root.returncode != 0:
        worker["code_island_error"] = (root.stderr or root.stdout or "not a git repository").strip()
        return
    repo_root = root.stdout.strip() or project_path

    if os.path.isdir(worktree_path):
        handled = _prepare_existing_code_island_worktree(
            worker,
            repo_root=repo_root,
            branch=branch,
            base_branch=base_branch,
        )
        if handled:
            return

    if os.path.isdir(worktree_path):
        return
    worker["code_island_ready"] = False
    worker["code_island_pending"] = True
    try:
        Path(worktree_path).parent.mkdir(parents=True, exist_ok=True)
        branch_exists = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", branch],
            cwd=repo_root,
            timeout=10,
        ).returncode == 0
        if branch_exists:
            cmd = ["git", "worktree", "add", worktree_path, branch]
        else:
            start_ref = _worktree_base_start_ref(repo_root, base_branch)
            cmd = ["git", "worktree", "add", "-b", branch, worktree_path, start_ref]
        result = subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True, timeout=120)
        if result.returncode == 0:
            worker["code_island_ready"] = True
            worker["code_island_pending"] = False
            worker.pop("code_island_error", None)
        else:
            worker["code_island_ready"] = False
            worker["code_island_pending"] = True
            worker["code_island_error"] = (result.stderr or result.stdout or "git worktree add failed").strip()
    except Exception as exc:
        worker["code_island_ready"] = False
        worker["code_island_pending"] = True
        worker["code_island_error"] = str(exc)


def ensure_code_island_for_board(board: str) -> bool:
    """Prepare a Discord worker board workspace before dispatch spawns roles."""
    started = time.time()
    worker = _read_worker_meta(board)
    if worker.get("kind") != "discord_worker_board":
        return True
    active_pipeline = (
        worker.get("execution_mode") == "kanban_pipeline"
        and worker.get("goal_status") == "active"
    )
    previous_telemetry_state = _code_island_telemetry_state(worker)
    previous_worktree_path = str(worker.get("worktree_path") or "").strip()
    _ensure_code_island(worker)
    code_island_updates = {
        key: worker[key]
        for key in (
            "code_island_ready",
            "code_island_pending",
            "worktree_path",
            "project_path",
        )
        if key in worker
    }
    code_island_updates["code_island_error"] = worker.get("code_island_error", _DELETE_META)

    def apply_code_island_updates(
        current_metadata: dict[str, Any],
        current_worker: dict[str, Any],
    ) -> bool:
        changed = False
        for key, value in code_island_updates.items():
            previous_value = current_worker.get(key)
            if value is _DELETE_META:
                if key in current_worker:
                    current_worker.pop(key, None)
                    changed = True
            elif previous_value != value:
                current_worker[key] = value
                changed = True
        return changed

    written = _mutate_worker_metadata(
        board,
        apply_code_island_updates,
        warning_action="ensure Discord worker board code island metadata",
    )
    if written is None:
        return False
    if previous_worktree_path != str(worker.get("worktree_path") or ""):
        _sync_role_task_workspaces(
            board,
            old_path=previous_worktree_path,
            new_path=str(worker.get("worktree_path") or ""),
        )
    elapsed_ms = int((time.time() - started) * 1000)
    blocker = _code_island_blocker(worker) if active_pipeline else ""
    health_error = _active_code_island_health_error(worker, active_pipeline=active_pipeline)
    if blocker:
        _block_worker_board_for_code_island(board, worker, blocker)
    telemetry_changed = previous_telemetry_state != _code_island_telemetry_state(worker)
    actionable_transition = telemetry_changed and not _is_terminal_worker_meta(worker)
    log = logger.info if health_error or blocker or actionable_transition else logger.debug
    log(
        "discord_worker_code_island board=%s ready=%s pending=%s elapsed_ms=%d error=%s",
        board,
        bool(worker.get("code_island_ready")),
        bool(worker.get("code_island_pending")),
        elapsed_ms,
        health_error,
    )
    if blocker:
        return False
    return bool(worker.get("code_island_ready") or not _code_island_configured(worker))


def find_board_by_share_token(token: str) -> Optional[str]:
    needle = str(token or "").strip()
    if not needle:
        return None
    for board in kanban_db.list_boards(include_archived=True):
        slug = str(board.get("slug") or kanban_db.DEFAULT_BOARD)
        worker = _read_worker_meta(slug)
        if secrets.compare_digest(str(worker.get("share_token") or ""), needle):
            return slug
    return None


def public_board_snapshot(token: str) -> dict[str, Any]:
    board = find_board_by_share_token(token)
    if not board:
        raise KeyError("unknown board token")
    return _public_board_snapshot_for_board(board)


def public_board_snapshot_for_session(session_id: str) -> dict[str, Any]:
    board = resolve_public_session_board(session_id)
    return _public_board_snapshot_for_board(board)


def _retry_public_read_after_corruption(board: str, exc: kanban_db.KanbanDbCorruptError) -> bool:
    """Run the existing conservative DB repair once for public worker views."""

    try:
        result = kanban_db.repair_corrupt_board(board)
    except Exception:
        logger.warning("public worker-board corruption repair failed for %s", board, exc_info=True)
        return False
    if result.get("status") == "repaired":
        logger.warning(
            "public worker-board corruption repaired for %s via %s after %s",
            board,
            result.get("action") or "unknown",
            exc.reason,
        )
        return True
    logger.warning(
        "public worker-board corruption repair unavailable for %s: %s",
        board,
        result.get("reason") or exc.reason,
    )
    return False


def _public_board_snapshot_for_board(board: str) -> dict[str, Any]:
    metadata = kanban_db.read_board_metadata(board)
    worker = dict(metadata.get(DISCORD_WORKER_META_KEY) or {})
    try:
        conn = kanban_db.connect(board=board)
    except kanban_db.KanbanDbCorruptError as exc:
        if not _retry_public_read_after_corruption(board, exc):
            raise
        conn = kanban_db.connect(board=board)
    try:
        tasks = kanban_db.list_tasks(conn, include_archived=False)
        summaries = kanban_db.latest_summaries(conn, [t.id for t in tasks])
        counts = kanban_db.board_stats(conn).get("by_status", {})
        running = _running_ticket_snapshot(conn)
        runtime = _board_runtime_snapshot(
            worker,
            counts=counts,
            running=running,
            conn=conn,
        )
        rows = []
        for task in tasks:
            rows.append(
                {
                    "id": task.id,
                    "title": task.title,
                    "status": task.status,
                    "assignee": task.assignee,
                    "priority": task.priority,
                    "created_at": task.created_at,
                    "completed_at": task.completed_at,
                    "body_preview": _public_task_body_preview(task.body),
                    "latest_summary": summaries.get(task.id),
                }
            )
    finally:
        conn.close()
    session_id = _public_session_id_for_board(board, worker)
    public_worker = _public_worker_meta(worker)
    public_url = public_session_board_url(session_id)
    if public_url:
        public_worker["public_url"] = public_url
    board_summary = read_board_run_summary(board)
    if not board_summary:
        try:
            state = board_thread_state(board)
        except Exception:
            state = ""
        if state in {"done", "blocked", "errored"}:
            try:
                board_summary = build_board_run_summary(board)
            except Exception:
                board_summary = {}
    return {
        "board": board,
        "name": _worker_board_name(worker, metadata, board),
        "description": metadata.get("description") or "",
        "session_id": session_id,
        "thread_id": str(worker.get("thread_id") or ""),
        "worker": public_worker,
        "counts": counts,
        "running": running,
        "runtime": runtime,
        "board_summary": board_summary,
        "tasks": rows,
    }


def ticket_state_for_session(session_id: str, task_id: str) -> dict[str, Any]:
    board = resolve_public_session_board(session_id)
    worker = _read_worker_meta(board)
    return _ticket_state_for_board(board, task_id, worker=worker)


def ticket_terminal_feed_for_session(session_id: str, task_id: str) -> dict[str, Any]:
    board = resolve_public_session_board(session_id)
    worker = _read_worker_meta(board)
    return _ticket_terminal_feed_for_board(board, task_id, worker=worker)


def worker_ticket_console_for_session(session_id: str, task_id: str) -> dict[str, Any]:
    """Return authenticated operator-console state for one worker ticket."""
    board = resolve_public_session_board(session_id)
    worker = _read_worker_meta(board)
    try:
        return _worker_ticket_console_for_board(board, task_id, worker=worker)
    except KeyError:
        return _worker_ticket_console_log_only_payload(board, task_id, worker=worker)


def worker_ticket_console_log_for_session(session_id: str, task_id: str) -> dict[str, Any]:
    """Return paths and snapshot data for the authenticated operator log."""
    board = resolve_public_session_board(session_id)
    worker = _read_worker_meta(board)
    try:
        task, runs, _events, current_run, codex_state = _ticket_console_parts(board, task_id)
        task_id = str(task.id)
        snapshot = _worker_ticket_console_payload(
            board,
            task,
            runs=runs,
            events=_events,
            current_run=current_run,
            codex_state=codex_state,
            worker=worker,
        )
    except KeyError:
        task_id = str(task_id or "").strip()
        snapshot = _worker_ticket_console_log_only_payload(board, task_id, worker=worker)
    return {
        "board": board,
        "task_id": task_id,
        "log_path": str(kanban_db.worker_log_path(task_id, board=board)),
        "state_path": str(codex_worker_state_path(task_id, board=board)),
        "snapshot": snapshot,
    }


def render_public_session_ticket_terminal_html(session_id: str, task_id: str) -> str:
    """Render a shareable, sanitized terminal page for one worker ticket."""
    feed = ticket_terminal_feed_for_session(session_id, task_id)
    worker = feed.get("worker") if isinstance(feed.get("worker"), dict) else {}
    task = feed.get("task") if isinstance(feed.get("task"), dict) else {}

    def esc(value: Any) -> str:
        return html.escape(str(value or ""))

    quoted_session = quote(str(session_id or ""), safe="")
    quoted_task = quote(str(task_id or ""), safe="")
    board_url = f"/workers/{quoted_session}"
    ticket_url = f"{board_url}/tickets/{quoted_task}"
    json_url = f"{ticket_url}/terminal.json"
    title = task.get("title") or task_id
    lines = feed.get("lines") if isinstance(feed.get("lines"), list) else []
    body = "\n".join(str(line) for line in lines) if lines else "(no terminal activity yet)"
    updated = _format_public_timestamp(feed.get("updated_at")) or "now"
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc(title)} - Terminal</title>
  <style>
{_workers_page_css()}
    .terminal-page {{ margin: 0 auto; max-width: 1040px; }}
    .terminal-head {{ align-items: flex-start; display: flex; flex-wrap: wrap; gap: 12px; justify-content: space-between; margin-bottom: 12px; }}
    .terminal-log {{ background: #111827; border: 1px solid #374151; border-radius: 8px; color: #f9fafb; font: 12px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; overflow: auto; padding: 16px; white-space: pre-wrap; word-break: break-word; }}
  </style>
</head>
<body>
  <header>
    <div class="top-nav">
      <a class="brand" href="/workers">Hermes<br>Kanban</a>
      <a class="back-link" href="{esc(ticket_url)}">Back to Ticket</a>
    </div>
    <div class="hero">
      <h1>{esc(title)}</h1>
      <div class="meta">
        <span>Ticket: <code>{esc(task.get("id") or task_id)}</code></span>
        <span>Status: {esc(task.get("status") or "unknown")}</span>
        <span>Assignee: {esc(task.get("assignee") or "unassigned")}</span>
        <span>Session: <code>{esc(worker.get("thread_id") or session_id)}</code></span>
        <span>Updated: {esc(updated)}</span>
      </div>
    </div>
  </header>
  <main class="terminal-page">
    <div class="terminal-head">
      <a class="button-link" href="{esc(board_url)}">Board</a>
      <a class="button-link" href="{esc(ticket_url)}">Ticket Modal</a>
      <a class="button-link" href="{esc(json_url)}">JSON Feed</a>
    </div>
    <pre class="terminal-log">{esc(body)}</pre>
  </main>
</body>
</html>"""


def move_ticket_for_session(session_id: str, task_id: str, status: str) -> dict[str, Any]:
    """Move one public worker-board ticket to another visible status column."""
    board = resolve_public_session_board(session_id)
    worker = _read_worker_meta(board)

    task_id = str(task_id or "").strip()
    new_status = str(status or "").strip().lower()
    if not task_id:
        raise KeyError("unknown ticket")
    if new_status not in PUBLIC_BOARD_COLUMNS:
        raise ValueError(f"unknown status: {new_status}")

    conn = kanban_db.connect(board=board)
    try:
        if kanban_db.get_task(conn, task_id) is None:
            raise KeyError("unknown ticket")
        ok = kanban_db.move_task_status(
            conn,
            task_id,
            new_status,
            source="workers-page",
        )
        if not ok:
            if new_status == "ready":
                blockers = kanban_db.parents_blocking_ready(conn, task_id)
                if blockers:
                    names = ", ".join(
                        f"{p['title']!r} ({p['id']}, status={p['status']})"
                        for p in blockers
                    )
                    raise TicketMoveConflict(
                        "Cannot move to 'ready': blocked by parent(s) "
                        f"not done - {names}"
                    )
            raise TicketMoveConflict(
                f"status transition to {new_status!r} not valid from current state"
            )
    finally:
        conn.close()

    snapshot = _public_board_snapshot_for_board(board)
    updated = next(
        (task for task in snapshot.get("tasks", []) if task.get("id") == task_id),
        None,
    )
    return {"ok": True, "task": updated, "snapshot": snapshot}


def _ticket_state_for_board(
    board: str,
    task_id: str,
    *,
    worker: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    task_id = str(task_id or "").strip()
    if not task_id:
        raise KeyError("unknown ticket")
    conn = kanban_db.connect(board=board)
    try:
        task = kanban_db.get_task(conn, task_id)
        if task is None:
            raise KeyError("unknown ticket")
        runs = kanban_db.list_runs(conn, task_id)
        events = kanban_db.list_events(conn, task_id)
        codex_state = _ticket_codex_state(task_id, board=board)
        payload = {
            "board": board,
            "worker": _public_worker_meta(worker or _read_worker_meta(board)),
            "task": _task_state_dict(task),
            "worker_run": _worker_run_profile_state(
                codex_state.get("result") if isinstance(codex_state, dict) else None
            ),
            "current_run": _current_run_state(task, runs),
            "runs": [_run_state_dict(run) for run in runs],
            "events": [_event_state_dict(event) for event in events[-200:]],
            "worker_log_tail": kanban_db.read_worker_log(
                task_id,
                tail_bytes=CODEX_STATE_LOG_TAIL_BYTES,
                board=board,
            ),
            "codex_state": codex_state,
        }
    finally:
        conn.close()
    return _redact_public_state(payload)


def _ticket_terminal_feed_for_board(
    board: str,
    task_id: str,
    *,
    worker: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    task_id = str(task_id or "").strip()
    if not task_id:
        raise KeyError("unknown ticket")
    conn = kanban_db.connect(board=board)
    try:
        task = kanban_db.get_task(conn, task_id)
        if task is None:
            raise KeyError("unknown ticket")
        runs = kanban_db.list_runs(conn, task_id)
        events = kanban_db.list_events(conn, task_id)
        current_run = _current_run_state(task, runs)
        log_text = kanban_db.read_worker_log(task_id, board=board)
        codex_state = _read_codex_worker_state(task_id, board=board)
    finally:
        conn.close()

    lines = _terminal_feed_lines(
        task,
        current_run=current_run,
        events=events,
        log_text=log_text,
        codex_state=codex_state,
    )
    payload = {
        "board": board,
        "worker": _public_worker_meta(worker or _read_worker_meta(board)),
        "task": {
            "id": task.id,
            "title": task.title,
            "status": task.status,
            "assignee": task.assignee,
        },
        "current_run": _public_terminal_run(current_run),
        "updated_at": _now(),
        "lines": lines,
    }
    return _redact_terminal_state(payload)


def _ticket_console_parts(board: str, task_id: str) -> tuple[Any, list[Any], list[Any], Optional[dict[str, Any]], dict[str, Any]]:
    task_id = str(task_id or "").strip()
    if not task_id:
        raise KeyError("unknown ticket")
    conn = kanban_db.connect(board=board)
    try:
        task = kanban_db.get_task(conn, task_id)
        if task is None:
            raise KeyError("unknown ticket")
        runs = kanban_db.list_runs(conn, task_id)
        events = kanban_db.list_events(conn, task_id)
        current_run = _current_run_state(task, runs)
        codex_state = _read_codex_worker_state(task_id, board=board)
    finally:
        conn.close()
    return task, runs, events, current_run, codex_state


def _worker_ticket_console_for_board(
    board: str,
    task_id: str,
    *,
    worker: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    task, runs, events, current_run, codex_state = _ticket_console_parts(board, task_id)
    return _worker_ticket_console_payload(
        board,
        task,
        runs=runs,
        events=events,
        current_run=current_run,
        codex_state=codex_state,
        worker=worker,
    )


def _worker_ticket_console_payload(
    board: str,
    task: Any,
    *,
    runs: list[Any],
    events: list[Any],
    current_run: Optional[dict[str, Any]],
    codex_state: dict[str, Any],
    worker: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    workspace_path = _ticket_workspace_path(task, board=board)
    workspace_available = bool(workspace_path and Path(workspace_path).expanduser().is_dir())
    log_text = kanban_db.read_worker_log(
        task.id,
        tail_bytes=CODEX_STATE_LOG_TAIL_BYTES,
        board=board,
    )
    return {
        "board": board,
        "worker": _public_worker_meta(worker or _read_worker_meta(board)),
        "task": _task_state_dict(task),
        "workspace": {
            "path": workspace_path,
            "kind": task.workspace_kind,
            "available": workspace_available,
        },
        "backend": _worker_state_backend(codex_state),
        "current_run": _public_terminal_run(current_run),
        "runs": [_run_state_dict(run) for run in runs[-20:]],
        "events": [_event_state_dict(event) for event in events[-200:]],
        "worker_log_path": str(kanban_db.worker_log_path(task.id, board=board)),
        "worker_log_tail": log_text or "",
        "codex_state": codex_state if isinstance(codex_state, dict) else {},
        "updated_at": _now(),
    }


def _worker_ticket_console_log_only_payload(
    board: str,
    task_id: str,
    *,
    worker: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    task_id = str(task_id or "").strip()
    if not task_id:
        raise KeyError("unknown ticket")
    log_path = kanban_db.worker_log_path(task_id, board=board)
    state_path = codex_worker_state_path(task_id, board=board)
    if not log_path.exists() and not state_path.exists():
        raise KeyError("unknown ticket")
    codex_state = _ticket_codex_state(task_id, board=board)
    log_text = kanban_db.read_worker_log(
        task_id,
        tail_bytes=CODEX_STATE_LOG_TAIL_BYTES,
        board=board,
    )
    return {
        "board": board,
        "worker": _public_worker_meta(worker or _read_worker_meta(board)),
        "task": {
            "id": task_id,
            "title": f"Retained worker activity for {task_id}",
            "body": "Task metadata is no longer present; showing retained worker log/state artifacts.",
            "assignee": None,
            "status": "log-only",
            "priority": 0,
            "created_by": None,
            "created_at": None,
            "started_at": None,
            "completed_at": None,
            "workspace_kind": "unknown",
            "workspace_path": "",
            "branch_name": None,
            "result": None,
            "claim_lock": None,
            "claim_expires": None,
            "worker_pid": None,
            "last_failure_error": None,
            "max_runtime_seconds": None,
            "last_heartbeat_at": None,
            "current_run_id": None,
            "model_override": None,
            "session_id": None,
        },
        "workspace": {
            "path": "",
            "kind": "unknown",
            "available": False,
        },
        "backend": _worker_state_backend(codex_state),
        "current_run": None,
        "runs": [],
        "events": [],
        "worker_log_path": str(log_path),
        "worker_log_tail": log_text or "",
        "codex_state": codex_state if isinstance(codex_state, dict) else {},
        "updated_at": _now(),
        "log_only": True,
    }


def _ticket_workspace_path(task: Any, *, board: str) -> str:
    raw_path = str(getattr(task, "workspace_path", "") or "").strip()
    if raw_path:
        return str(Path(raw_path).expanduser())
    kind = str(getattr(task, "workspace_kind", "") or "scratch")
    if kind == "scratch":
        return str(kanban_db.workspaces_root(board=board) / str(task.id))
    if kind == "worktree":
        return str(Path.cwd() / ".worktrees" / str(task.id))
    return ""


def _public_terminal_run(run: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not run:
        return None
    keys = (
        "id",
        "status",
        "outcome",
        "worker_pid",
        "started_at",
        "last_heartbeat_at",
        "ended_at",
        "summary",
        "error",
        "metadata",
    )
    return {key: run.get(key) for key in keys}


def _safe_terminal_text(value: Any, *, max_chars: int = 240) -> str:
    text = str(value or "").strip().replace("\r", " ").replace("\n", " ")
    if len(text) > max_chars:
        return f"{text[:max_chars].rstrip()}..."
    return text


def _redact_terminal_state(value: Any) -> Any:
    """Redact credentials for authenticated operator terminal views."""
    if isinstance(value, str):
        from agent.redact import redact_sensitive_text

        return redact_sensitive_text(value, force=True)
    if isinstance(value, list):
        return [_redact_terminal_state(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_terminal_state(item) for key, item in value.items()}
    return value


def _terminal_feed_lines(
    task: Any,
    *,
    current_run: Optional[dict[str, Any]],
    events: list[Any],
    log_text: Optional[str],
    codex_state: Optional[dict[str, Any]],
) -> list[str]:
    lines = [
        f"$ ticket {task.id}",
        f"title: {_safe_terminal_text(task.title)}",
        f"status: {_safe_terminal_text(task.status)}",
    ]
    if task.assignee:
        lines.append(f"assignee: {_safe_terminal_text(task.assignee)}")
    worker_run = _worker_run_profile_line(
        (codex_state or {}).get("result") if isinstance(codex_state, dict) else None
    )
    if worker_run:
        lines.append(f"worker_run: {worker_run}")
    if current_run:
        run_bits = [
            f"run={current_run.get('id') or '-'}",
            f"status={current_run.get('status') or current_run.get('outcome') or '-'}",
        ]
        if current_run.get("worker_pid"):
            run_bits.append(f"pid={current_run.get('worker_pid')}")
        started = _format_public_timestamp(current_run.get("started_at"))
        heartbeat = _format_public_timestamp(current_run.get("last_heartbeat_at"))
        ended = _format_public_timestamp(current_run.get("ended_at"))
        if started:
            run_bits.append(f"started={started}")
        if heartbeat:
            run_bits.append(f"heartbeat={heartbeat}")
        if ended:
            run_bits.append(f"ended={ended}")
        lines.append("run: " + " ".join(run_bits))
    else:
        lines.append("run: not started")

    event_lines: list[str] = []
    for event in events[-40:]:
        line = _terminal_event_line(event)
        if line:
            event_lines.append(line)
    if event_lines:
        lines.append("")
        lines.append("# lifecycle")
        lines.extend(event_lines)

    public_log_lines = _public_worker_log_lines(log_text)
    if public_log_lines:
        lines.append("")
        lines.append("# worker terminal")
        lines.extend(public_log_lines)
    elif log_text:
        lines.append("")
        lines.append("# worker terminal")
        lines.append("(worker process output hidden)")
    codex_log_lines = _codex_app_worker_log_lines(codex_state)
    if codex_log_lines:
        lines.append("")
        lines.append(f"# {_worker_state_log_label(codex_state)}")
        lines.extend(codex_log_lines)
    else:
        diagnostics: list[str] = []
        if not current_run:
            diagnostics.append("worker run has not started yet")
        if not public_log_lines and not log_text:
            diagnostics.append("no worker stdout/stderr log has been captured yet")
        diagnostics.append(
            "no Codex app-server internals, state, or event log has been captured yet"
        )
        if diagnostics:
            lines.append("")
            lines.append("# diagnostics")
            lines.extend(f"- {line}" for line in diagnostics)
    return lines


def _terminal_event_line(event: Any) -> str:
    kind = str(getattr(event, "kind", "") or "").strip()
    if not kind:
        return ""
    created = _format_public_timestamp(getattr(event, "created_at", None)) or "time unknown"
    payload = getattr(event, "payload", None) if isinstance(getattr(event, "payload", None), dict) else {}
    if kind == "claimed":
        return f"[{created}] claimed by worker"
    if kind == "spawned":
        pid = payload.get("pid") if isinstance(payload, dict) else None
        return f"[{created}] spawned worker pid={pid}" if pid else f"[{created}] spawned worker"
    if kind == "completed":
        summary = _safe_terminal_text(payload.get("summary") if isinstance(payload, dict) else "")
        return f"[{created}] completed: {summary}" if summary else f"[{created}] completed"
    if kind == "blocked":
        reason = _safe_terminal_text(payload.get("reason") if isinstance(payload, dict) else "")
        return f"[{created}] blocked: {reason}" if reason else f"[{created}] blocked"
    if kind in {"reclaimed", "archived", "scheduled", "promoted", "assigned"}:
        return f"[{created}] {kind}"
    if kind in {"spawn_failed", "crashed", "timed_out", "gave_up"}:
        reason = _safe_terminal_text(payload.get("error") if isinstance(payload, dict) else "")
        label = kind.replace("_", " ")
        return f"[{created}] {label}: {reason}" if reason else f"[{created}] {label}"
    return ""


def _public_worker_log_lines(log_text: Optional[str]) -> list[str]:
    if not log_text:
        return []
    lines: list[str] = []
    for raw in str(log_text).splitlines():
        line = raw.rstrip()
        if not line.strip():
            lines.append("")
            continue
        for label in ("Codex", "OpenCode"):
            marker = f"spawning {label} role worker"
            if marker in line:
                prefix = line.split(marker, 1)[0]
                line = prefix + f"{marker}: [command hidden]"
                break
        lines.append(_safe_terminal_text(line, max_chars=1200))
    return lines


def _worker_state_backend(state: Optional[dict[str, Any]]) -> str:
    if not isinstance(state, dict):
        return "codex"
    result = state.get("result") if isinstance(state.get("result"), dict) else {}
    backend = str(result.get("backend") or "").strip().lower()
    if backend in {"codex", "opencode"}:
        return backend
    events = state.get("events") if isinstance(state.get("events"), list) else []
    for event in events:
        if not isinstance(event, dict):
            continue
        method = str(event.get("method") or "").strip().lower()
        if method.startswith("opencode/"):
            return "opencode"
    return "codex"


def _worker_state_log_label(state: Optional[dict[str, Any]]) -> str:
    if _worker_state_backend(state) == "opencode":
        return "opencode worker log"
    return "codex app worker log"


def _codex_app_worker_log_lines(state: Optional[dict[str, Any]]) -> list[str]:
    if not isinstance(state, dict) or not state:
        return []
    lines: list[str] = []
    backend = _worker_state_backend(state)
    backend_label = "opencode" if backend == "opencode" else "codex app"
    truncated = int(state.get("truncated_events") or 0)
    if truncated > 0:
        lines.append(f"... {truncated} older {backend_label} event(s) truncated by retention")
    events = state.get("events") if isinstance(state.get("events"), list) else []
    for event in events:
        line = _codex_app_event_line(event, backend=backend)
        if line:
            lines.append(line)
    result_line = _codex_app_result_line(state.get("result"))
    if result_line:
        lines.append(result_line)
    return lines


def _codex_app_event_line(event: Any, *, backend: str = "codex") -> str:
    if not isinstance(event, dict):
        return ""
    method = str(event.get("method") or "").strip()
    if not method:
        return ""
    created = _format_public_timestamp(event.get("ts")) or "time unknown"
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
    item = params.get("item") if isinstance(params.get("item"), dict) else {}
    item_type = str(event.get("item_type") or item.get("type") or "").strip()
    item_label = item_type or _codex_method_item_label(method)
    backend_label = "opencode" if backend == "opencode" else "codex"
    parts = [f"[{created}] {backend_label} {method}"]
    if item_label:
        parts.append(item_label)

    if method.endswith("/outputDelta") or method.endswith("/delta"):
        parts.append("output hidden")
        return _codex_log_line(parts)

    turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
    status = item.get("status") or turn.get("status")
    if status:
        parts.append(f"status={_safe_terminal_text(status, max_chars=80)}")
    if item_type == "commandExecution":
        cwd = item.get("cwd")
        exit_code = item.get("exitCode")
        duration = item.get("durationMs")
        if cwd:
            parts.append(f"cwd={_safe_terminal_text(cwd, max_chars=260)}")
        if exit_code is not None:
            parts.append(f"exit={exit_code}")
        if duration is not None:
            parts.append(f"duration={duration}ms")
        if item.get("aggregatedOutput"):
            parts.append("output hidden")
    elif item_type == "fileChange":
        summary = _codex_file_change_summary(item)
        if summary:
            parts.append(summary)
    elif item_type in {"agentMessage", "reasoning", "userMessage"}:
        parts.append("content hidden")
    elif item_type in {"mcpToolCall", "dynamicToolCall"}:
        tool = item.get("tool") or item.get("name")
        server = item.get("server")
        if server:
            parts.append(f"server={_safe_terminal_text(server, max_chars=80)}")
        if tool:
            parts.append(f"tool={_safe_terminal_text(tool, max_chars=120)}")
        parts.append("result hidden")

    error_obj = turn.get("error")
    if isinstance(error_obj, dict):
        message = error_obj.get("message") or error_obj.get("code")
        if message:
            parts.append(f"error={_safe_terminal_text(message, max_chars=260)}")
    return _codex_log_line(parts)


def _codex_log_line(parts: list[Any]) -> str:
    if len(parts) <= 1:
        return str(parts[0]) if parts else ""
    return ": ".join([str(parts[0]), " ".join(str(part) for part in parts[1:])])


def _codex_method_item_label(method: str) -> str:
    if method.startswith("item/"):
        bits = [bit for bit in method.split("/") if bit]
        if len(bits) >= 2:
            return bits[1]
    return ""


def _codex_file_change_summary(item: dict[str, Any]) -> str:
    changes = item.get("changes") if isinstance(item.get("changes"), list) else []
    if not changes:
        return "changes=0"
    labels: list[str] = []
    for change in changes[:12]:
        if not isinstance(change, dict):
            continue
        kind_obj = change.get("kind") if isinstance(change.get("kind"), dict) else {}
        kind = str(kind_obj.get("type") or change.get("type") or "update")
        path = str(change.get("path") or "")
        labels.append(f"{kind}:{path}" if path else kind)
    if len(changes) > len(labels):
        labels.append(f"+{len(changes) - len(labels)} more")
    return _safe_terminal_text(
        f"changes={len(changes)} " + ", ".join(labels),
        max_chars=1200,
    )


def _codex_app_result_line(result: Any) -> str:
    if not isinstance(result, dict):
        return ""
    backend = str(result.get("backend") or "codex").strip().lower()
    label = "opencode" if backend == "opencode" else "codex"
    bits = [f"{label} result"]
    if result.get("error"):
        bits.append(f"error={_safe_terminal_text(result.get('error'), max_chars=260)}")
    if result.get("interrupted"):
        bits.append("interrupted=true")
    if result.get("timed_out"):
        bits.append("timed_out=true")
    if result.get("tool_iterations") not in (None, ""):
        bits.append(f"tool_iterations={result.get('tool_iterations')}")
    if result.get("turn_id"):
        bits.append(f"turn={_safe_terminal_text(result.get('turn_id'), max_chars=80)}")
    if result.get("thread_id"):
        bits.append(f"thread={_safe_terminal_text(result.get('thread_id'), max_chars=80)}")
    return " ".join(bits) if len(bits) > 1 else ""


def _worker_run_profile_state(result: Any) -> Optional[dict[str, Any]]:
    if not isinstance(result, dict):
        return None
    summary = _worker_run_profile_line(result)
    if not summary:
        return None
    return {
        "summary": summary,
        "profile": result.get("run_profile") if isinstance(result.get("run_profile"), dict) else {},
        "service_tier": result.get("service_tier"),
        "fast_mode": result.get("fast_mode"),
    }


def _worker_run_profile_line(result: Any) -> str:
    if not isinstance(result, dict):
        return ""
    profile = result.get("run_profile") if isinstance(result.get("run_profile"), dict) else {}
    passes = profile.get("passes") if isinstance(profile.get("passes"), list) else []
    label = str(profile.get("label") or "").strip()
    if not label and passes:
        names = [str(item.get("name") or "").strip() for item in passes if isinstance(item, dict)]
        names = [name for name in names if name]
        if len(names) == 2 and names == ["plan", "build"]:
            label = "2-pass plan+build"
        elif len(names) == 1 and names[0] == "build":
            label = "1-pass simple build"
        elif len(names) == 1:
            label = f"1-pass {names[0]}"
    if not label:
        return ""

    bits = [label]
    for item in passes:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("agent") or "").strip()
        reasoning = _format_worker_reasoning(item.get("reasoning"))
        if name and reasoning:
            bits.append(f"{name} reasoning={reasoning}")

    service_tier = str(result.get("service_tier") or "").strip().lower()
    fast_raw = result.get("fast_mode")
    fast_mode: Optional[bool]
    if isinstance(fast_raw, bool):
        fast_mode = fast_raw
    elif service_tier:
        fast_mode = service_tier == "fast"
    else:
        fast_mode = None
    if fast_mode is not None:
        bits.append(f"fast mode={'on' if fast_mode else 'off'}")
    return _safe_terminal_text("; ".join(bits), max_chars=400)


def _format_worker_reasoning(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    if raw == "xhigh":
        return "x-high"
    return raw


def _public_task_body_preview(value: Any, *, max_chars: int = 420) -> str:
    """Return a compact, public-safe ticket brief for board cards."""
    text = str(value or "").strip()
    if not text:
        return ""
    redacted = _redact_public_state(text)
    text = str(redacted or "").strip()
    text = re.sub(r"\s+", " ", text)
    if len(text) > max_chars:
        return f"{text[:max_chars].rstrip()}..."
    return text


def _task_state_dict(task: Any) -> dict[str, Any]:
    return {
        "id": task.id,
        "title": task.title,
        "body": task.body,
        "assignee": task.assignee,
        "status": task.status,
        "priority": task.priority,
        "created_by": task.created_by,
        "created_at": task.created_at,
        "started_at": task.started_at,
        "completed_at": task.completed_at,
        "workspace_kind": task.workspace_kind,
        "workspace_path": task.workspace_path,
        "branch_name": task.branch_name,
        "result": task.result,
        "claim_lock": task.claim_lock,
        "claim_expires": task.claim_expires,
        "worker_pid": task.worker_pid,
        "last_failure_error": task.last_failure_error,
        "max_runtime_seconds": task.max_runtime_seconds,
        "last_heartbeat_at": task.last_heartbeat_at,
        "current_run_id": task.current_run_id,
        "model_override": task.model_override,
        "session_id": task.session_id,
    }


def _run_state_dict(run: Any) -> dict[str, Any]:
    return {
        "id": run.id,
        "task_id": run.task_id,
        "profile": run.profile,
        "step_key": run.step_key,
        "status": run.status,
        "claim_lock": run.claim_lock,
        "claim_expires": run.claim_expires,
        "worker_pid": run.worker_pid,
        "max_runtime_seconds": run.max_runtime_seconds,
        "last_heartbeat_at": run.last_heartbeat_at,
        "started_at": run.started_at,
        "ended_at": run.ended_at,
        "outcome": run.outcome,
        "summary": run.summary,
        "metadata": run.metadata,
        "error": run.error,
    }


def _event_state_dict(event: Any) -> dict[str, Any]:
    return {
        "id": event.id,
        "task_id": event.task_id,
        "run_id": event.run_id,
        "kind": event.kind,
        "payload": event.payload,
        "created_at": event.created_at,
    }


def _redact_public_state(value: Any) -> Any:
    if isinstance(value, str):
        from agent.redact import redact_sensitive_text

        text = _WINDOWS_PATH_RE.sub("[REDACTED_PATH]", value)
        text = _POSIX_PATH_RE.sub("[REDACTED_PATH]", text)
        return redact_sensitive_text(text, force=True)
    if isinstance(value, list):
        return [_redact_public_state(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_public_state(item) for key, item in value.items()}
    return value


def _public_worker_meta(worker: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "kind",
        "thread_id",
        "initial_request",
        "root_goal",
        "summary_title",
        "concise_outcome",
        "goal_status",
        "phase",
        "execution_mode",
        "criteria",
        "worker_branch",
        "review_loop_count",
        "review_loop_limit",
        "public_url",
        "pr_url",
        "pr_number",
        "paused",
        "cancelled",
        "blocked_reason",
        "created_at",
        "updated_at",
    }
    public = {key: value for key, value in worker.items() if key in allowed}
    discord_url = _discord_thread_url(worker)
    if discord_url:
        public["discord_thread_url"] = discord_url
    return public


def _clean_feature_summary_text(
    value: Any,
    *,
    max_chars: int,
    default: str = "",
) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip()) or default
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _worker_generated_title(worker: dict[str, Any]) -> str:
    return _clean_feature_summary_text(
        worker.get("summary_title"),
        max_chars=80,
        default="",
    )


def _fallback_feature_title(worker: dict[str, Any]) -> str:
    source = str(worker.get("root_goal") or worker.get("initial_request") or "").strip()
    match = re.match(r"^/goal(?:\s+(.*))?$", source, re.IGNORECASE | re.DOTALL)
    if match and (match.group(1) or "").strip().lower() not in GOAL_CONTROL_COMMANDS:
        source = (match.group(1) or "").strip()
    return _clean_feature_summary_text(
        source,
        max_chars=80,
        default="Discord Worker Feature",
    )


def _worker_index_title(worker: dict[str, Any], fallback: Any) -> str:
    return (
        _worker_generated_title(worker)
        or _clean_feature_summary_text(
            worker.get("root_goal") or worker.get("initial_request") or fallback,
            max_chars=160,
            default="Discord Worker Board",
        )
    )


def _worker_board_name(worker: dict[str, Any], metadata: dict[str, Any], board: str) -> str:
    return _worker_generated_title(worker) or str(metadata.get("name") or board)


def set_feature_summary_title(board: str, title: str) -> str:
    """Persist the generated feature title used by embeds and /workers."""
    cleaned = _clean_feature_summary_text(title, max_chars=80, default="")
    if not board or not cleaned:
        return ""
    worker = _read_worker_meta(board)
    if worker.get("kind") != "discord_worker_board":
        return ""
    if _worker_generated_title(worker) == cleaned:
        return cleaned
    worker["summary_title"] = cleaned
    _update_worker_meta(board, worker)
    return cleaned


def _format_public_timestamp(value: Any) -> str:
    try:
        ts = int(value)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(ts))


def _public_status_text(worker: dict[str, Any]) -> str:
    status = str(worker.get("goal_status") or "").strip()
    phase = str(worker.get("phase") or "").strip()
    if status and phase and status != phase:
        return f"{status} / {phase}"
    return status or phase or "pending"


def _public_board_summary_html(summary: dict[str, Any]) -> str:
    if not summary:
        return ""
    text = render_board_run_summary_text(summary)
    return (
        '<section class="criteria board-summary">'
        '<strong>Terminal Summary</strong>'
        f'<pre>{html.escape(text)}</pre>'
        '</section>'
    )


def _public_review_text(worker: dict[str, Any]) -> str:
    count = worker.get("review_loop_count")
    limit = _review_loop_limit_for_worker(worker)
    return f"{count or 0}/{limit}"


def _public_count_text(counts: dict[str, Any]) -> str:
    if not counts:
        return "none"
    ordered = ["triage", "todo", "scheduled", "ready", "running", "blocked", "review", "done"]
    keys = [key for key in ordered if key in counts]
    keys.extend(sorted(key for key in counts if key not in ordered))
    return " ".join(f"{key}:{counts.get(key) or 0}" for key in keys)


def _worker_config() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
        return dict((cfg.get("kanban") or {}).get("discord_worker") or {})
    except Exception:
        return {}


def _kanban_config() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        return dict((load_config() or {}).get("kanban") or {})
    except Exception:
        return {}


def _active_role_count_across_boards() -> int:
    total = 0
    for board_meta in kanban_db.list_boards(include_archived=False):
        board = str(board_meta.get("slug") or kanban_db.DEFAULT_BOARD)
        worker = _read_worker_meta(board)
        if worker.get("kind") != "discord_worker_board":
            continue
        with kanban_db.connect_closing(board=board) as conn:
            total += len(_running_ticket_snapshot(conn))
    return total


def board_thread_state(board: str) -> str:
    """Return the Discord thread-facing state for a worker board."""
    worker = _read_worker_meta(board)
    if worker.get("cancelled") or worker.get("goal_status") == "cancelled":
        return "errored"
    with kanban_db.connect_closing(board=board) as conn:
        tasks = kanban_db.list_tasks(conn, include_archived=False)
        counts = kanban_db.board_stats(conn).get("by_status", {})

    stale_blocked_meta = _worker_meta_allows_ready_dispatch(worker, counts=counts)
    terminal_state = "" if stale_blocked_meta else _terminal_worker_reaction_state(worker)
    is_terminal = terminal_state in {"done", "blocked", "errored"}
    has_worker_blocker = (
        not stale_blocked_meta
        and not _pr_finalizer_failure_is_pending_checks(worker)
        and (
            bool(str(worker.get("blocked_reason") or "").strip())
            or worker.get("goal_status") == "blocked"
        )
    )

    if tasks:
        blocked_tasks = [task for task in tasks if task.status == "blocked"]
        if blocked_tasks:
            # The public Discord marker should reflect the current board
            # state, not the run outcome that explained how it got there.
            # A crashed/timed-out role worker parks the ticket in Kanban's
            # blocked lane for operator attention; leaving the source
            # message on hourglass hides that stall, while showing an error
            # implies a terminal failure rather than a human-actionable blocker.
            return "blocked"
        if terminal_state in {"blocked", "errored"}:
            return terminal_state
        if terminal_state == "running":
            return "running"
        if is_terminal and all(task.status == "done" for task in tasks):
            return terminal_state or "done"
        if any(task.status == "running" for task in tasks):
            return "running"
        return "active"

    if is_terminal:
        return terminal_state or "done"
    if terminal_state == "running":
        return "running"
    if has_worker_blocker:
        return "blocked"
    return "active"


def board_thread_reaction_state(board: str) -> str:
    """Return the Discord reaction state for a worker board.

    ``active`` covers both never-started queues and boards that are between
    worker claims. Reactions need to distinguish those: once any ticket has
    started or completed, keep the visible marker in the working state instead
    of falling back to the pickup/queued eyes marker.
    """
    state = board_thread_state(board)
    if state != "active":
        return state

    with kanban_db.connect_closing(board=board) as conn:
        tasks = kanban_db.list_tasks(conn, include_archived=False)
        if any(getattr(task, "started_at", None) or getattr(task, "completed_at", None) for task in tasks):
            return "running"
    return "active"


def source_task_reaction_state(board: str, task_id: str) -> Optional[str]:
    """Return Discord reaction/status state for a source Kanban task."""
    board = str(board or "").strip() or kanban_db.DEFAULT_BOARD
    task_id = str(task_id or "").strip()
    if not task_id:
        return None
    with kanban_db.connect_closing(board=board) as conn:
        task = kanban_db.get_task(conn, task_id)
        if task is None:
            return None
        latest = kanban_db.latest_run(conn, task_id)
        active = kanban_db.active_run(conn, task_id)

    status = str(getattr(task, "status", "") or "").strip().lower()
    latest_status = str(getattr(latest, "status", "") or "").strip().lower() if latest else ""
    latest_outcome = str(getattr(latest, "outcome", "") or "").strip().lower() if latest else ""
    latest_error_like = latest_status in _SOURCE_TASK_ERROR_OUTCOMES or latest_outcome in _SOURCE_TASK_ERROR_OUTCOMES

    if status == "done":
        return "done"
    if latest_error_like:
        return "errored"
    if status == "blocked":
        if str(getattr(task, "last_failure_error", "") or "").strip():
            return "errored"
        return "blocked"
    active_status = str(getattr(active, "status", "") or "").strip().lower() if active else ""
    if status == "running" and active_status == "running":
        return "running"
    if status in {"triage", "todo", "scheduled", "ready", "review"}:
        return "active"
    if status == "archived":
        return "blocked"
    return "active"


def _foreman_source_task_from_request(text: str) -> dict[str, str]:
    source: dict[str, str] = {}
    mapping = {
        "board": "source_board",
        "task": "source_task_id",
    }
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip().lstrip("- ").strip()
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        target = mapping.get(key.strip().casefold())
        if target and value.strip():
            source[target] = value.strip()[:300]
    return source


def _worker_source_task_context(worker: dict[str, Any]) -> dict[str, str]:
    context = {
        "source_board": str(worker.get("source_board") or "").strip(),
        "source_task_id": str(worker.get("source_task_id") or "").strip(),
    }
    request_text = str(worker.get("root_goal") or worker.get("initial_request") or "")
    if (
        (not context["source_board"] or not context["source_task_id"])
        and _is_foreman_generated_request(request_text)
    ):
        parsed = _foreman_source_task_from_request(request_text)
        context["source_board"] = context["source_board"] or parsed.get("source_board", "")
        context["source_task_id"] = context["source_task_id"] or parsed.get("source_task_id", "")
    return {key: value for key, value in context.items() if value}


def _is_foreman_generated_worker(worker: dict[str, Any]) -> bool:
    request = str(
        worker.get("initial_request")
        or worker.get("root_goal")
        or worker.get("latest_planner_request")
        or ""
    )
    return _is_foreman_generated_request(request)


def _foreman_source_board_from_request(text: str) -> str:
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip().lstrip("- ").strip()
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        if key.strip().casefold() == "board":
            return value.strip()[:120]
    return ""


def _active_foreman_source_boards() -> set[str]:
    sources: set[str] = set()
    for board_meta in kanban_db.list_boards(include_archived=False):
        board = str(board_meta.get("slug") or kanban_db.DEFAULT_BOARD)
        worker = _read_worker_meta(board)
        if worker.get("kind") != "discord_worker_board" or not _is_foreman_generated_worker(worker):
            continue
        if worker.get("cancelled") or str(worker.get("goal_status") or "").strip().lower() in TERMINAL_GOAL_STATUSES:
            continue
        source = _foreman_source_board_from_request(
            str(worker.get("initial_request") or worker.get("root_goal") or "")
        )
        if source:
            sources.add(source)
    return sources


def _latest_task_summaries(tasks: list[Any], summaries: dict[str, str]) -> list[str]:
    ordered = sorted(
        tasks,
        key=lambda task: (
            getattr(task, "completed_at", None)
            or getattr(task, "started_at", None)
            or getattr(task, "created_at", 0)
            or 0
        ),
        reverse=True,
    )
    out: list[str] = []
    for task in ordered:
        summary = _clean_feature_summary_text(
            summaries.get(getattr(task, "id", "")),
            max_chars=220,
            default="",
        )
        if summary:
            out.append(summary)
        if len(out) >= 2:
            break
    return out


def _feature_summary_outcome(
    worker: dict[str, Any],
    *,
    state: str,
    tasks: list[Any],
    summaries: dict[str, str],
    counts: dict[str, Any],
    running: list[dict[str, Any]],
) -> str:
    stored = _clean_feature_summary_text(
        worker.get("concise_outcome"),
        max_chars=420,
        default="",
    )
    if stored:
        return stored

    latest = _latest_task_summaries(tasks, summaries)
    blocked_tasks = [task for task in tasks if getattr(task, "status", "") == "blocked"]
    blocker = _clean_feature_summary_text(
        worker.get("blocked_reason"),
        max_chars=320,
        default="",
    )
    if not blocker and blocked_tasks:
        task = blocked_tasks[0]
        blocker = _clean_feature_summary_text(
            getattr(task, "last_failure_error", None)
            or summaries.get(getattr(task, "id", ""))
            or getattr(task, "title", ""),
            max_chars=320,
            default="",
        )

    if state == "errored":
        return _clean_feature_summary_text(
            f"Failed. {blocker}" if blocker else "Failed. A Kanban worker hit an unrecoverable error.",
            max_chars=420,
        )
    if state == "blocked":
        return _clean_feature_summary_text(
            f"Blocked. {blocker}" if blocker else "Blocked. Kanban is waiting on input or a failed ticket.",
            max_chars=420,
        )
    if state == "done":
        detail = " ".join(latest)
        return _clean_feature_summary_text(
            f"Done. {detail}" if detail else "Done. Kanban work completed.",
            max_chars=420,
        )

    running_text = _running_status_text(running)
    if running and running_text != "idle":
        return _clean_feature_summary_text(
            f"In progress. {running_text}",
            max_chars=420,
        )
    if latest:
        return _clean_feature_summary_text(
            "In progress. " + " ".join(latest),
            max_chars=420,
        )

    queued = int(counts.get("ready") or 0) + int(counts.get("todo") or 0) + int(counts.get("review") or 0)
    if queued:
        return f"In progress. {queued} Kanban ticket{'s' if queued != 1 else ''} queued."
    if int(counts.get("done") or 0):
        return "In progress. Completed tickets are awaiting final board reconciliation."
    return "In progress. Kanban pipeline is preparing work."


def _status_counts(counts: dict[str, Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for status in PUBLIC_BOARD_COLUMNS:
        out[status] = int(counts.get(status) or 0)
    for key, value in counts.items():
        if key not in out:
            out[str(key)] = int(value or 0)
    out["total"] = sum(value for key, value in out.items() if key != "total")
    return out


def _count_values(values: list[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        label = str(value or "unknown").strip().lower() or "unknown"
        counts[label] = counts.get(label, 0) + 1
    return counts


def _clean_summary_value(value: Any, *, default: str = "unknown") -> str:
    text = str(value or "").strip()
    return text if text else default


def _task_sort_timestamp(task: Any) -> int:
    return int(
        getattr(task, "completed_at", None)
        or getattr(task, "started_at", None)
        or getattr(task, "created_at", None)
        or 0
    )


def _run_sort_timestamp(run: Any) -> int:
    return int(
        getattr(run, "ended_at", None)
        or getattr(run, "started_at", None)
        or 0
    )


def _normalized_review_verdict_status(value: Any) -> str:
    status = re.sub(r"[\s-]+", "_", str(value or "").strip().lower())
    aliases = {
        "approve": "approved",
        "approved": "approved",
        "pass": "approved",
        "passed": "approved",
        "ok": "approved",
        "clean": "approved",
        "changes_requested": "changes_requested",
        "change_requested": "changes_requested",
        "needs_changes": "changes_requested",
        "needs_revision": "changes_requested",
        "needs_fix": "changes_requested",
        "fix_required": "changes_requested",
        "blocked": "blocked",
        "rejected": "rejected",
    }
    return aliases.get(status, "")


def _review_verdict_status_from_metadata(metadata: dict[str, Any]) -> str:
    for key in ("raw", "parsed"):
        payload = metadata.get(key)
        if not isinstance(payload, dict):
            continue
        for status_key in ("status", "verdict", "decision"):
            status = _normalized_review_verdict_status(payload.get(status_key))
            if status:
                return status
    for status_key in ("verdict", "decision", "review_status", "status"):
        status = _normalized_review_verdict_status(metadata.get(status_key))
        if status:
            return status
    return ""


def _final_reviewer_verdict(tasks: list[Any], runs_by_task: dict[str, list[Any]]) -> dict[str, str]:
    reviewer_tasks = [
        task for task in tasks
        if str(getattr(task, "assignee", "") or "").strip().lower() == ROLE_REVIEWER
    ]
    for task in sorted(reviewer_tasks, key=_task_sort_timestamp, reverse=True):
        for run in sorted(runs_by_task.get(getattr(task, "id", ""), []), key=_run_sort_timestamp, reverse=True):
            metadata = getattr(run, "metadata", None) if isinstance(getattr(run, "metadata", None), dict) else {}
            status = _review_verdict_status_from_metadata(metadata)
            if status:
                return {
                    "status": status,
                    "task_id": str(getattr(task, "id", "") or ""),
                    "run_id": str(getattr(run, "id", "") or ""),
                    "summary": str(getattr(run, "summary", "") or ""),
                }
            summary = str(getattr(run, "summary", "") or "").strip()
            if summary:
                return {
                    "status": "unknown",
                    "task_id": str(getattr(task, "id", "") or ""),
                    "run_id": str(getattr(run, "id", "") or ""),
                    "summary": summary,
                }
    return {"status": "unknown", "task_id": "", "run_id": "", "summary": ""}


def _verification_commands(tasks: list[Any], runs_by_task: dict[str, list[Any]]) -> list[dict[str, Any]]:
    commands: list[dict[str, Any]] = []
    seen_commands: set[str] = set()
    task_by_id = {str(getattr(task, "id", "") or ""): task for task in tasks}
    candidates: list[tuple[int, int, int, int, str, Any, Any, dict[str, Any]]] = []

    for task_id, runs in runs_by_task.items():
        task = task_by_id.get(task_id)
        assignee = str(getattr(task, "assignee", "") or "").strip().lower() if task else ""
        if assignee not in {ROLE_DEV, ROLE_REVIEWER}:
            continue
        for run in runs:
            metadata = getattr(run, "metadata", None) if isinstance(getattr(run, "metadata", None), dict) else {}
            tests = metadata.get("tests")
            if not isinstance(tests, list):
                raw = metadata.get("raw") if isinstance(metadata.get("raw"), dict) else {}
                raw_tests = raw.get("tests") if isinstance(raw, dict) else []
                tests = raw_tests if isinstance(raw_tests, list) else []
            if not isinstance(tests, list):
                tests = []
            for index, item in enumerate(tests):
                if not isinstance(item, dict):
                    continue
                command = str(item.get("command") or "").strip()
                if not command:
                    continue
                run_id = int(getattr(run, "id", None) or 0)
                role_priority = 1 if assignee == ROLE_REVIEWER else 0
                candidates.append((_run_sort_timestamp(run), run_id, role_priority, -index, task_id, task, run, item))

    for _timestamp, _run_id, _role_priority, _order, task_id, task, run, item in sorted(candidates, reverse=True):
        command = str(item.get("command") or "").strip()
        command_key = command.casefold()
        if command_key in seen_commands:
            continue
        seen_commands.add(command_key)
        commands.append(
            {
                "task_id": task_id,
                "task_title": str(getattr(task, "title", "") or "") if task else "",
                "run_id": getattr(run, "id", None),
                "command": command,
                "result": str(item.get("result") or "unknown").strip() or "unknown",
                "output": _cap_state_value(str(item.get("output") or ""), max_text=4000),
            }
        )
        if len(commands) >= 20:
            break
    return commands


def _latest_failure_or_blocker(worker: dict[str, Any], tasks: list[Any], runs_by_task: dict[str, list[Any]]) -> str:
    reason = str(worker.get("blocked_reason") or "").strip()
    if reason:
        return reason
    blocked = [task for task in tasks if str(getattr(task, "status", "") or "") == "blocked"]
    for task in sorted(blocked, key=_task_sort_timestamp, reverse=True):
        error = str(getattr(task, "last_failure_error", "") or "").strip()
        if error:
            return error
        for run in sorted(runs_by_task.get(getattr(task, "id", ""), []), key=_run_sort_timestamp, reverse=True):
            run_error = str(getattr(run, "error", "") or "").strip()
            if run_error:
                return run_error
            summary = str(getattr(run, "summary", "") or "").strip()
            if summary:
                return summary
    pr_blocker = str(worker.get("pr_blocker") or worker.get("pr_error") or "").strip()
    return pr_blocker or ""


def _summary_timestamps(worker: dict[str, Any], tasks: list[Any], runs: list[Any]) -> tuple[Optional[int], Optional[int], Optional[int]]:
    starts = [worker.get("created_at")]
    starts.extend(getattr(task, "created_at", None) for task in tasks)
    starts.extend(getattr(run, "started_at", None) for run in runs)
    started = min((int(value) for value in starts if value), default=None)
    terminal = str(worker.get("goal_status") or "").strip().lower() in TERMINAL_GOAL_STATUSES
    completed = int(worker.get("updated_at") or _now()) if terminal else None
    duration = (completed - started) if started and completed and completed >= started else None
    return started, completed, duration


def _run_phase_name(task: Any, run: Any) -> str:
    assignee = str(getattr(task, "assignee", "") or "").strip().lower()
    unit = str(getattr(run, "worker_unit", "") or "").strip().lower()
    step = str(getattr(run, "step_key", "") or "").strip().lower()
    joined = " ".join(part for part in (assignee, unit, step) if part)
    if assignee == ROLE_PLANNER or "planner" in joined or "plan" in joined:
        return "Plan"
    if assignee == ROLE_REVIEWER or "reviewer" in joined or "review" in joined:
        return "Review"
    if "ci" in joined or "check" in joined or "test" in joined:
        return "CI"
    if "deploy" in joined or "release" in joined:
        return "Deploy"
    if assignee == ROLE_DEV or "dev" in joined or "build" in joined:
        return "Build"
    return "Work"


def _board_runtime_breakdown(
    worker: dict[str, Any],
    tasks: list[Any],
    runs_by_task: dict[str, list[Any]],
) -> dict[str, Any]:
    runs = [run for run_list in runs_by_task.values() for run in run_list]
    started_at, completed_at, duration_seconds = _summary_timestamps(worker, tasks, runs)
    if started_at and completed_at is None:
        duration_seconds = max(0, _now() - int(started_at))
    task_by_id = {str(getattr(task, "id", "") or ""): task for task in tasks}
    phases: dict[str, dict[str, Any]] = {}
    active = 0.0
    now = _now()
    for run in runs:
        start = getattr(run, "started_at", None)
        if start is None:
            continue
        try:
            start_i = int(start)
        except (TypeError, ValueError):
            continue
        end_raw = getattr(run, "ended_at", None)
        try:
            end_i = int(end_raw) if end_raw is not None else now
        except (TypeError, ValueError):
            end_i = now
        if end_i < start_i:
            continue
        duration = float(end_i - start_i)
        task = task_by_id.get(str(getattr(run, "task_id", "") or ""))
        phase = _run_phase_name(task, run)
        slot = phases.setdefault(phase, {"name": phase, "duration_s": 0.0, "count": 0})
        slot["duration_s"] += duration
        slot["count"] += 1
        active += duration
    wall = float(duration_seconds or 0)
    queue = max(0.0, wall - active) if wall else 0.0
    rows = sorted(phases.values(), key=lambda item: item["duration_s"], reverse=True)
    if queue:
        rows.append({"name": "Queue", "duration_s": queue, "count": 0})
    return {
        "schema_version": 1,
        "scope": "discord_worker_board",
        "wall_s": wall,
        "model_s": 0.0,
        "tools_s": active,
        "overhead_s": queue,
        "active_s": active,
        "api_calls": 0,
        "tool_calls": len(runs),
        "phases": rows[:6],
        "active_exceeds_wall": bool(wall and active > wall + 0.25),
    }


def _pr_merge_evidence_present(*, state: str, merged_at: str, merge_commit: str) -> bool:
    return state.upper() == "MERGED" or bool(merged_at or merge_commit)


def _worker_has_successful_pr_terminal_evidence(worker: dict[str, Any]) -> bool:
    context = worker.get("project_context") if isinstance(worker.get("project_context"), dict) else {}
    amend = context.get("github_pr_amend") if isinstance(context.get("github_pr_amend"), dict) else {}
    requires_head_sha_advance = amend.get("requires_head_sha_advance") is True
    if requires_head_sha_advance and worker.get("pr_amend_head_advanced") is not True:
        return False
    if requires_head_sha_advance:
        upstream_head = str(worker.get("pr_amend_upstream_head_sha") or "").strip()
        trigger_head = str(worker.get("pr_amend_trigger_head_sha") or amend.get("head_sha") or "").strip()
        if not upstream_head or not trigger_head or upstream_head == trigger_head:
            worker.setdefault(
                "pr_blocker",
                "PR-amend completion blocked: missing post-push upstream PR head SHA verification.",
            )
            return False
    state = str(worker.get("pr_state") or "").strip()
    merged_at = str(worker.get("pr_merged_at") or "").strip()
    merge_commit = str(worker.get("pr_merge_commit") or "").strip()
    if not _pr_merge_evidence_present(state=state, merged_at=merged_at, merge_commit=merge_commit):
        return False
    checks_status = str(worker.get("pr_checks_status") or "").strip().lower()
    if checks_status not in {"passed", "success"}:
        return False
    return not [item for item in worker.get("pr_checks_failed") or [] if str(item).strip()]


def _normalized_pr_summary_merge_state(
    *,
    state: str,
    merged_at: str,
    merge_commit: str,
    merge_state: str,
) -> str:
    raw = str(merge_state or "").strip()
    raw_upper = raw.upper()
    if _pr_merge_evidence_present(
        state=state,
        merged_at=merged_at,
        merge_commit=merge_commit,
    ) and raw_upper in {
        "",
        "UNKNOWN",
        "MERGED",
    }:
        return "merged"
    return raw or "unknown"


def _deployment_summary_status(worker: dict[str, Any], pr: dict[str, Any]) -> str:
    explicit = str(worker.get("deployment_status") or "").strip()
    explicit_lower = explicit.lower()
    if explicit and explicit_lower not in {"unknown", "not checked", "unchecked"}:
        return explicit
    checks_status = str(pr.get("checks_status") or "").strip().lower()
    if (
        str(pr.get("merge_state") or "").strip().lower() == "merged"
        and checks_status in {"passed", "success"}
        and not str(pr.get("blocker") or pr.get("error") or pr.get("status_error") or "").strip()
    ):
        return "done"
    return explicit or "not checked"


def _pr_summary(worker: dict[str, Any]) -> dict[str, Any]:
    checks_status = str(worker.get("pr_checks_status") or "").strip() or "not checked"
    state = str(worker.get("pr_state") or "").strip() or "unknown"
    merged_at = str(worker.get("pr_merged_at") or "").strip()
    merge_commit = str(worker.get("pr_merge_commit") or "").strip()
    merge_state = str(worker.get("pr_merge_state") or "").strip()
    merge_state = _normalized_pr_summary_merge_state(
        state=state,
        merged_at=merged_at,
        merge_commit=merge_commit,
        merge_state=merge_state,
    )
    pr_success = _worker_has_successful_pr_terminal_evidence(worker)
    return {
        "url": str(worker.get("pr_url") or "").strip(),
        "number": str(worker.get("pr_number") or "").strip(),
        "error": "" if pr_success else str(worker.get("pr_error") or "").strip(),
        "status_error": "" if pr_success else str(worker.get("pr_status_error") or "").strip(),
        "state": state,
        "merged_at": merged_at,
        "merge_commit": merge_commit,
        "merge_state": merge_state,
        "mergeable": worker.get("pr_mergeable") if worker.get("pr_mergeable") is not None else "unknown",
        "is_draft": worker.get("pr_is_draft") if worker.get("pr_is_draft") is not None else "unknown",
        "review_decision": str(worker.get("pr_review_decision") or "").strip() or "unknown",
        "checks_status": checks_status,
        "checks_total": int(worker.get("pr_checks_total") or 0),
        "checks_failed": list(worker.get("pr_checks_failed") or []),
        "ci_wait_state": str(worker.get("pr_ci_wait_state") or "").strip(),
        "ci_wait_started_at": worker.get("pr_ci_wait_started_at"),
        "ci_next_poll_at": worker.get("pr_ci_next_poll_at"),
        "ci_wait_seconds": int(worker.get("pr_ci_wait_seconds") or 0),
        "blocker": "" if pr_success else str(worker.get("pr_blocker") or "").strip(),
    }


def build_board_run_summary(board: str) -> dict[str, Any]:
    """Build a deterministic summary of a Discord worker board's terminal state."""
    metadata = kanban_db.read_board_metadata(board)
    worker = dict(metadata.get(DISCORD_WORKER_META_KEY) or {})
    if worker.get("kind") != "discord_worker_board":
        raise KeyError("unknown Discord worker board")

    conn = kanban_db.connect(board=board)
    try:
        tasks = kanban_db.list_tasks(conn, include_archived=False)
        counts = _status_counts(kanban_db.board_stats(conn).get("by_status", {}))
        summaries = kanban_db.latest_summaries(conn, [t.id for t in tasks])
        runs_by_task = {t.id: kanban_db.list_runs(conn, t.id) for t in tasks}
    finally:
        conn.close()

    runs = [run for run_list in runs_by_task.values() for run in run_list]
    started_at, completed_at, duration_seconds = _summary_timestamps(worker, tasks, runs)
    runtime_breakdown = _board_runtime_breakdown(worker, tasks, runs_by_task)
    task_rows = [
        {
            "id": str(getattr(task, "id", "") or ""),
            "title": str(getattr(task, "title", "") or ""),
            "assignee": str(getattr(task, "assignee", "") or "unassigned"),
            "status": str(getattr(task, "status", "") or "unknown"),
            "last_failure_error": str(getattr(task, "last_failure_error", "") or ""),
            "latest_summary": str(summaries.get(getattr(task, "id", "")) or ""),
        }
        for task in sorted(tasks, key=_task_sort_timestamp, reverse=True)[:20]
    ]
    blocker = _latest_failure_or_blocker(worker, tasks, runs_by_task)
    pr = _pr_summary(worker)
    summary = {
        "schema_version": BOARD_RUN_SUMMARY_SCHEMA_VERSION,
        "board": board,
        "generated_at": _now(),
        "thread_id": str(worker.get("thread_id") or ""),
        "chat_id": str(worker.get("chat_id") or worker.get("thread_id") or ""),
        "title": _worker_generated_title(worker) or _fallback_feature_title(worker),
        "root_goal": str(worker.get("root_goal") or worker.get("initial_request") or ""),
        "outcome": _clean_feature_summary_text(worker.get("concise_outcome"), max_chars=420, default=""),
        "goal_status": _clean_summary_value(worker.get("goal_status")),
        "phase": _clean_summary_value(worker.get("phase")),
        "thread_state": board_thread_state(board),
        "blocked_reason": blocker,
        "task_counts": counts,
        "run_counts": {
            "total": len(runs),
            "by_status": _count_values([getattr(run, "status", None) for run in runs]),
            "by_outcome": _count_values([getattr(run, "outcome", None) for run in runs]),
        },
        "review": {
            "loop_count": int(worker.get("review_loop_count") or 0),
            "loop_limit": _review_loop_limit_for_worker(worker),
            "final_verdict": _final_reviewer_verdict(tasks, runs_by_task),
        },
        "pr": pr,
        "deployment_status": _deployment_summary_status(worker, pr),
        "verification_commands": _verification_commands(tasks, runs_by_task),
        "final_response": {
            "text": str(worker.get("final_discord_response") or ""),
            "recorded_at": worker.get("final_discord_response_at"),
            "session_id": str(worker.get("final_discord_session_id") or ""),
            "work_item_id": str(worker.get("final_discord_work_item_id") or ""),
            "message_id": str(worker.get("final_discord_message_id") or ""),
        },
        "branch": str(worker.get("worker_branch") or "").strip(),
        "public_url": str(worker.get("public_url") or "").strip(),
        "started_at": started_at,
        "completed_at": completed_at,
        "duration_seconds": duration_seconds,
        "runtime_breakdown": runtime_breakdown,
        "latest_tasks": task_rows,
    }
    return summary


def persist_board_run_summary(board: str) -> dict[str, Any]:
    """Persist and index the deterministic terminal summary for a board."""
    summary = build_board_run_summary(board)
    path = board_run_summary_path(board)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json_write(path, summary, indent=2)

    def mutate(metadata: dict[str, Any], worker: dict[str, Any]) -> bool:
        if worker.get("kind") != "discord_worker_board":
            return False
        worker["board_summary"] = summary
        worker["board_summary_path"] = str(path)
        worker["board_summary_updated_at"] = summary["generated_at"]
        return True

    _mutate_worker_metadata(board, mutate, warning_action="persist Discord board run summary metadata")
    return summary


def record_final_discord_response(
    board: str,
    *,
    final_response: str,
    session_id: Optional[str] = None,
    work_item_id: Optional[str] = None,
    result_message_id: Optional[str] = None,
) -> None:
    """Persist final Discord-response provenance on a worker board."""
    if not board:
        return
    worker = _read_worker_meta(board)
    if worker.get("kind") != "discord_worker_board":
        return

    def mutate(metadata: dict[str, Any], current_worker: dict[str, Any]) -> bool:
        if current_worker.get("kind") != "discord_worker_board":
            return False
        current_worker["final_discord_response"] = _cap_state_value(str(final_response or ""), max_text=12000)
        current_worker["final_discord_response_at"] = _now()
        if session_id:
            current_worker["final_discord_session_id"] = str(session_id)
        if work_item_id:
            current_worker["final_discord_work_item_id"] = str(work_item_id)
        if result_message_id:
            current_worker["final_discord_message_id"] = str(result_message_id)
        current_worker["terminal_summary_sync_pending"] = True
        _arm_terminal_completion_notice_if_ready(current_worker)
        return True

    if _mutate_worker_metadata(board, mutate, warning_action="record final Discord response metadata") is None:
        return
    try:
        persist_board_run_summary(board)
    except Exception:
        logger.debug("Failed to refresh Discord board run summary for %s", board, exc_info=True)
    try:
        mark_dispatch_dirty(board=board, reason="final-discord-response-recorded")
    except Exception:
        logger.debug("Failed to mark Discord worker dispatch dirty for %s", board, exc_info=True)


def _is_current_board_run_summary(summary: dict[str, Any], board: str) -> bool:
    if not isinstance(summary, dict) or summary.get("board") != board:
        return False
    try:
        schema_version = int(summary.get("schema_version") or 0)
    except (TypeError, ValueError):
        schema_version = 0
    return schema_version >= BOARD_RUN_SUMMARY_SCHEMA_VERSION


def read_board_run_summary(board: str) -> dict[str, Any]:
    """Return the indexed persisted run summary, if the current run has one."""
    worker = _read_worker_meta(board)
    indexed_at = worker.get("board_summary_updated_at")
    if not indexed_at:
        return {}
    embedded = worker.get("board_summary") if isinstance(worker.get("board_summary"), dict) else {}
    if (
        embedded
        and embedded.get("generated_at") == indexed_at
        and _is_current_board_run_summary(embedded, board)
    ):
        return dict(embedded)
    path = Path(str(worker.get("board_summary_path") or board_run_summary_path(board)))
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if _is_current_board_run_summary(loaded, board):
        return loaded
    return {}


def board_run_summary_for_session(session_id: str) -> dict[str, Any]:
    board = resolve_public_session_board(session_id)
    try:
        return read_board_run_summary(board) or build_board_run_summary(board)
    except kanban_db.KanbanDbCorruptError as exc:
        if not _retry_public_read_after_corruption(board, exc):
            raise
        return read_board_run_summary(board) or build_board_run_summary(board)


def _format_summary_counts(counts: dict[str, Any]) -> str:
    bits = [
        f"{status}:{counts.get(status) or 0}"
        for status in PUBLIC_BOARD_COLUMNS
        if int(counts.get(status) or 0)
    ]
    return " ".join(bits) or "none"


def _format_run_outcomes(run_counts: dict[str, Any]) -> str:
    outcomes = run_counts.get("by_outcome") if isinstance(run_counts.get("by_outcome"), dict) else {}
    bits = [f"{key}:{value}" for key, value in sorted(outcomes.items()) if value]
    return ", ".join(bits) or "none"


def _review_verdict_display_status(verdict: dict[str, Any]) -> str:
    status = str(verdict.get("status") or "").strip() or "unknown"
    if status != "unknown":
        return status
    summary = str(verdict.get("summary") or "").strip().lower()
    if summary.startswith("approved"):
        return "approved"
    if summary.startswith("changes requested") or summary.startswith("change requested"):
        return "changes_requested"
    if summary:
        return "recorded"
    return "unknown"


def _review_line(review: dict[str, Any], verdict: dict[str, Any]) -> str:
    status = _review_verdict_display_status(verdict)
    line = f"Review: {review.get('loop_count') or 0}/{review.get('loop_limit') or 'unknown'}; final verdict: {status}"
    note = _clean_feature_summary_text(verdict.get("summary"), max_chars=260, default="")
    if note:
        line = f"{line} — {note}"
    return line


def render_board_run_summary_text(summary: dict[str, Any]) -> str:
    """Render deterministic terminal-board facts for Discord and diagnostics."""
    pr = summary.get("pr") if isinstance(summary.get("pr"), dict) else {}
    review = summary.get("review") if isinstance(summary.get("review"), dict) else {}
    verdict = review.get("final_verdict") if isinstance(review.get("final_verdict"), dict) else {}
    pr_ref = pr.get("url") or pr.get("error") or "not opened"
    checks = pr.get("checks_status") or "not checked"
    merge = pr.get("merge_state") or "unknown"
    merge_commit = pr.get("merge_commit") or ""
    ci_wait_state = str(pr.get("ci_wait_state") or "").strip()
    lines = [
        f"Kanban goal: {summary.get('goal_status') or 'unknown'} / {summary.get('phase') or 'unknown'}",
        f"Board: {summary.get('public_url') or summary.get('board') or 'unknown'}",
        f"Branch: {summary.get('branch') or 'unknown'}",
        f"PR: {pr_ref}",
        f"PR merge: {merge}; checks: {checks}",
        f"Deployment: {summary.get('deployment_status') or 'not checked'}",
        *([f"Outcome: {summary.get('outcome')}"] if summary.get("outcome") else []),
        f"Tasks: {_format_summary_counts(summary.get('task_counts') if isinstance(summary.get('task_counts'), dict) else {})}",
        f"Runs: total={(summary.get('run_counts') or {}).get('total', 0) if isinstance(summary.get('run_counts'), dict) else 0}; outcomes: {_format_run_outcomes(summary.get('run_counts') if isinstance(summary.get('run_counts'), dict) else {})}",
        _review_line(review, verdict),
    ]
    blocker = str(summary.get("blocked_reason") or pr.get("blocker") or "").strip()
    if ci_wait_state:
        wait_seconds = int(pr.get("ci_wait_seconds") or 0)
        lines.append(f"CI gate: waiting ({ci_wait_state}; {wait_seconds}s elapsed)")
    if blocker:
        lines.append(f"Blocker: {blocker}")
    if merge_commit:
        lines.append(f"Merge commit: {merge_commit}")
    commands = summary.get("verification_commands") if isinstance(summary.get("verification_commands"), list) else []
    if commands:
        rendered = "; ".join(
            f"{item.get('command')} [{item.get('result') or 'unknown'}]"
            for item in commands[:5]
            if isinstance(item, dict) and item.get("command")
        )
        if rendered:
            lines.append(f"Verification: {rendered}")
    started = _format_public_timestamp(summary.get("started_at")) or "unknown"
    completed = _format_public_timestamp(summary.get("completed_at")) or "unknown"
    duration = summary.get("duration_seconds")
    duration_text = f"{duration}s" if duration is not None else "unknown"
    lines.append(f"Timing: started {started}; completed {completed}; duration {duration_text}")
    runtime_text = render_runtime_breakdown_text(summary.get("runtime_breakdown"), compact=True)
    if runtime_text:
        lines.append(f"Runtime: {runtime_text.replace(chr(10), '; ')}")
    return "\n".join(lines)


def _terminal_summary_outcome(summary: dict[str, Any]) -> str:
    status = str(summary.get("goal_status") or summary.get("thread_state") or "unknown").strip()
    pr = summary.get("pr") if isinstance(summary.get("pr"), dict) else {}
    task_counts = summary.get("task_counts") if isinstance(summary.get("task_counts"), dict) else {}
    bits = [f"{status.title()}. Tasks: {_format_summary_counts(task_counts)}."]
    if pr.get("url"):
        bits.append(f"PR: {pr.get('url')}.")
    elif pr.get("error"):
        bits.append(f"PR blocked: {pr.get('error')}.")
    bits.append(f"Checks: {pr.get('checks_status') or 'not checked'}.")
    if pr.get("ci_wait_state"):
        bits.append(f"CI gate: waiting for {pr.get('ci_wait_state')}.")
    deployment = summary.get("deployment_status") or "not checked"
    bits.append(f"Deployment: {deployment}.")
    blocker = str(summary.get("blocked_reason") or pr.get("blocker") or "").strip()
    if blocker:
        bits.append(f"Blocker: {blocker}.")
    return _clean_feature_summary_text(" ".join(bits), max_chars=420)


def feature_summary_snapshot(board: str) -> dict[str, Any]:
    """Return the current feature-summary values for a Discord worker board."""
    metadata = kanban_db.read_board_metadata(board)
    worker = dict(metadata.get(DISCORD_WORKER_META_KEY) or {})
    if worker.get("kind") != "discord_worker_board":
        raise KeyError("unknown Discord worker board")

    with kanban_db.connect_closing(board=board) as conn:
        tasks = kanban_db.list_tasks(conn, include_archived=False)
        summaries = kanban_db.latest_summaries(conn, [t.id for t in tasks])
        counts = kanban_db.board_stats(conn).get("by_status", {})
        running = _running_ticket_snapshot(conn)
        runs_by_task = {t.id: kanban_db.list_runs(conn, t.id) for t in tasks}

    state = board_thread_state(board)
    terminal_summary = read_board_run_summary(board)
    if state in {"done", "blocked", "errored"} and not terminal_summary:
        try:
            terminal_summary = persist_board_run_summary(board)
        except Exception:
            terminal_summary = {}
    title = _worker_generated_title(worker)
    outcome = _feature_summary_outcome(
        worker,
        state=state,
        tasks=tasks,
        summaries=summaries,
        counts=counts,
        running=running,
    )
    if terminal_summary:
        outcome = _terminal_summary_outcome(terminal_summary)
        runtime_breakdown = terminal_summary.get("runtime_breakdown") if isinstance(terminal_summary.get("runtime_breakdown"), dict) else {}
    else:
        runtime_breakdown = _board_runtime_breakdown(
            worker,
            tasks,
            runs_by_task,
        )
    snapshot = {
        "board": board,
        "thread_id": str(worker.get("thread_id") or ""),
        "chat_id": str(worker.get("chat_id") or worker.get("thread_id") or ""),
        "message_id": str(worker.get("summary_message_id") or "").strip(),
        "source_message_id": str(worker.get("source_message_id") or "").strip(),
        "guild_id": str(worker.get("guild_id") or "").strip(),
        "parent_channel_id": str(worker.get("parent_channel_id") or "").strip(),
        "state": state,
        "title": title,
        "fallback_title": _fallback_feature_title(worker),
        "outcome": outcome,
        "branch": str(worker.get("worker_branch") or "").strip(),
        "pr_url": str(worker.get("pr_url") or "").strip(),
        "pr_number": str(worker.get("pr_number") or "").strip(),
        "public_url": str(worker.get("public_url") or "").strip(),
        "board_summary": terminal_summary,
        "runtime_breakdown": runtime_breakdown,
        "terminal_summary_updated_at": terminal_summary.get("generated_at") if terminal_summary else None,
        "updated_at": worker.get("updated_at"),
    }
    key_payload = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    snapshot["sync_key"] = hashlib.sha256(key_payload.encode("utf-8")).hexdigest()
    return snapshot


def thread_status_targets() -> list[dict[str, Any]]:
    """Return Discord thread targets with their current board state."""
    targets: list[dict[str, Any]] = []
    active_foreman_sources = _active_foreman_source_boards()
    try:
        from hermes_cli.discord_worker_foreman import active_master_foreman_source_boards

        active_foreman_sources.update(active_master_foreman_source_boards())
    except Exception:
        pass
    seen_boards: set[str] = set()
    board_metas = kanban_db.list_boards(include_archived=False)
    board_metas.extend(_archived_terminal_completion_notice_boards())
    for board_meta in board_metas:
        board = str(board_meta.get("slug") or kanban_db.DEFAULT_BOARD)
        if board in seen_boards:
            continue
        worker = _worker_meta_from_board_meta(board, board_meta)
        terminal_status_sync_needed = _terminal_worker_status_sync_needed(worker)
        if _board_target_skipped_by_metadata(
            board,
            board_meta,
            source="discord thread status targets",
        ) and not terminal_status_sync_needed:
            continue
        seen_boards.add(board)
        if worker.get("kind") != "discord_worker_board":
            continue
        thread_id = str(worker.get("thread_id") or "").strip()
        if not thread_id:
            continue
        if _worker_source_message_too_old(worker):
            _clear_stale_terminal_sync_flags(board, worker)
            continue
        try:
            summary = _feature_summary_snapshot_for_status(board, board_meta, worker)
        except Exception as exc:
            if _is_skippable_board_db_error(exc):
                _log_skipped_board_target(board, exc, source="discord thread status feature summary")
                continue
            try:
                summary = {"state": _board_thread_state_for_status(board, board_meta, worker)}
            except Exception as state_exc:
                if _is_skippable_board_db_error(state_exc):
                    _log_skipped_board_target(board, state_exc, source="discord thread status state fallback")
                    continue
                raise
        state = summary.get("state")
        if not state:
            try:
                state = _board_thread_state_for_status(board, board_meta, worker)
            except Exception as exc:
                if _is_skippable_board_db_error(exc):
                    _log_skipped_board_target(board, exc, source="discord thread status board state")
                    continue
                raise
        source_context = _worker_source_task_context(worker)
        source_state = None
        if source_context.get("source_board") and source_context.get("source_task_id"):
            source_board = source_context["source_board"]
            try:
                source_state = source_task_reaction_state(
                    source_board,
                    source_context["source_task_id"],
                )
            except Exception as exc:
                if _is_skippable_board_db_error(exc):
                    _log_skipped_board_target(source_board, exc, source="discord thread status source task")
                    continue
                raise
        visible_state = state if state in {"done", "blocked", "errored"} else source_state or state
        is_terminal_worker = _is_terminal_worker_meta(worker)
        terminal_completion_message_pending = bool(worker.get("terminal_completion_message_pending"))
        terminal_sync_pending = bool(
            worker.get("terminal_reaction_sync_pending")
            or worker.get("terminal_summary_sync_pending")
            or terminal_completion_message_pending
        )
        reaction_state = ""
        terminal_reaction_sync_needed = False
        if is_terminal_worker and state in {"done", "blocked", "errored"}:
            try:
                reaction_state = _board_thread_reaction_state_for_status(board, board_meta, worker)
            except Exception as exc:
                if _is_skippable_board_db_error(exc):
                    _log_skipped_board_target(board, exc, source="discord thread status reaction state")
                    continue
                raise
            terminal_reaction_sync_needed = (
                reaction_state in {"done", "blocked", "errored"}
                and reaction_state != _terminal_reaction_synced_state(worker)
            )
        non_terminal_attention_state = (
            not is_terminal_worker
            and state in {"blocked", "errored"}
        )
        if (
            visible_state not in {"active", "running"}
            and not source_state
            and not non_terminal_attention_state
            and not (
                is_terminal_worker
                and state in {"done", "blocked", "errored"}
                and (terminal_sync_pending or terminal_reaction_sync_needed)
            )
            and board not in active_foreman_sources
        ):
            continue
        if source_state and state not in {"done", "blocked", "errored"}:
            reaction_state = source_state
        elif not reaction_state:
            try:
                reaction_state = _board_thread_reaction_state_for_status(board, board_meta, worker)
            except Exception as exc:
                if _is_skippable_board_db_error(exc):
                    _log_skipped_board_target(board, exc, source="discord thread status reaction state")
                    continue
                raise
        target = {
            "board": board,
            "thread_id": thread_id,
            "chat_id": str(worker.get("chat_id") or thread_id),
            "message_id": summary.get("message_id") or str(worker.get("summary_message_id") or ""),
            "source_message_id": summary.get("source_message_id") or str(worker.get("source_message_id") or ""),
            "guild_id": summary.get("guild_id") or str(worker.get("guild_id") or ""),
            "parent_channel_id": summary.get("parent_channel_id") or str(worker.get("parent_channel_id") or ""),
            "state": visible_state,
            "title": summary.get("title") or "",
            "fallback_title": summary.get("fallback_title") or "",
            "outcome": summary.get("outcome") or "",
            "branch": summary.get("branch") or "",
            "pr_url": summary.get("pr_url") or "",
            "pr_number": summary.get("pr_number") or "",
            "public_url": summary.get("public_url") or "",
            "board_summary": summary.get("board_summary") if isinstance(summary.get("board_summary"), dict) else {},
            "runtime_breakdown": summary.get("runtime_breakdown") or {},
            "sync_key": summary.get("sync_key") or "",
            "terminal_reaction_sync_pending": bool(worker.get("terminal_reaction_sync_pending")),
            "terminal_summary_sync_pending": bool(worker.get("terminal_summary_sync_pending")),
            "terminal_completion_message_pending": terminal_completion_message_pending,
            "terminal_reaction_sync_needed": terminal_reaction_sync_needed,
            "archived": bool(board_meta.get("archived")),
            "metadata_path": str(board_meta.get("metadata_path") or ""),
        }
        project_context = worker.get("project_context") if isinstance(worker.get("project_context"), dict) else {}
        github_pr_amend = project_context.get("github_pr_amend") if isinstance(project_context.get("github_pr_amend"), dict) else {}
        if github_pr_amend:
            target["github_pr_amend"] = dict(github_pr_amend)
        if source_context:
            target.update(source_context)
        if source_state:
            target["source_state"] = source_state
        if reaction_state != visible_state:
            target["reaction_state"] = reaction_state
        if board in active_foreman_sources and not source_state:
            target["reaction_state"] = "foreman"
        if _is_foreman_generated_worker(worker):
            target["hide_source_links"] = True
            target["foreman_generated"] = True
        targets.append(target)
    return targets


def _archived_terminal_completion_notice_boards() -> list[dict[str, Any]]:
    """Return archived terminal boards that still owe a Discord final response."""
    boards: list[dict[str, Any]] = []
    try:
        candidates = kanban_db.list_boards(include_archived=True)
    except Exception:
        return boards
    for board_meta in candidates:
        if not board_meta.get("archived"):
            continue
        board = str(board_meta.get("slug") or "").strip()
        if not board:
            continue
        worker = board_meta.get(DISCORD_WORKER_META_KEY) if isinstance(board_meta.get(DISCORD_WORKER_META_KEY), dict) else {}
        if worker.get("kind") != "discord_worker_board":
            continue
        if _terminal_worker_reaction_state(worker) != "done":
            continue
        if not worker.get("terminal_completion_message_pending"):
            continue
        summary = worker.get("board_summary") if isinstance(worker.get("board_summary"), dict) else {}
        final_response = summary.get("final_response") if isinstance(summary.get("final_response"), dict) else {}
        if not str(final_response.get("text") or "").strip():
            continue
        boards.append(board_meta)
    return boards


def _worker_meta_from_board_meta(board: str, board_meta: dict[str, Any]) -> dict[str, Any]:
    raw = board_meta.get(DISCORD_WORKER_META_KEY)
    if isinstance(raw, dict) and board_meta.get("archived"):
        return dict(raw)
    return _read_worker_meta(board)


def _read_board_run_summary_for_status(board: str, board_meta: dict[str, Any], worker: dict[str, Any]) -> dict[str, Any]:
    if not board_meta.get("archived"):
        return read_board_run_summary(board)
    embedded = worker.get("board_summary") if isinstance(worker.get("board_summary"), dict) else {}
    if embedded and _is_current_board_run_summary(embedded, board):
        return embedded
    path = Path(str(board_meta.get("archived_path") or "")) / BOARD_RUN_SUMMARY_FILENAME
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return loaded if _is_current_board_run_summary(loaded, board) else {}


def _feature_summary_snapshot_for_status(board: str, board_meta: dict[str, Any], worker: dict[str, Any]) -> dict[str, Any]:
    if not board_meta.get("archived"):
        return feature_summary_snapshot(board)
    terminal_summary = _read_board_run_summary_for_status(board, board_meta, worker)
    state = _terminal_worker_reaction_state(worker) or str(terminal_summary.get("goal_status") or "") or "done"
    outcome = _terminal_summary_outcome(terminal_summary) if terminal_summary else "Done."
    snapshot = {
        "board": board,
        "thread_id": str(worker.get("thread_id") or ""),
        "chat_id": str(worker.get("chat_id") or worker.get("thread_id") or ""),
        "message_id": str(worker.get("summary_message_id") or "").strip(),
        "source_message_id": str(worker.get("source_message_id") or "").strip(),
        "guild_id": str(worker.get("guild_id") or "").strip(),
        "parent_channel_id": str(worker.get("parent_channel_id") or "").strip(),
        "state": state,
        "title": _worker_generated_title(worker),
        "fallback_title": _fallback_feature_title(worker),
        "outcome": outcome,
        "branch": str(worker.get("worker_branch") or "").strip(),
        "pr_url": str(worker.get("pr_url") or "").strip(),
        "pr_number": str(worker.get("pr_number") or "").strip(),
        "public_url": str(worker.get("public_url") or "").strip(),
        "board_summary": terminal_summary,
        "runtime_breakdown": terminal_summary.get("runtime_breakdown") if isinstance(terminal_summary.get("runtime_breakdown"), dict) else {},
        "terminal_summary_updated_at": terminal_summary.get("generated_at") if terminal_summary else None,
        "updated_at": worker.get("updated_at"),
    }
    key_payload = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    snapshot["sync_key"] = hashlib.sha256(key_payload.encode("utf-8")).hexdigest()
    return snapshot


def _board_thread_state_for_status(board: str, board_meta: dict[str, Any], worker: dict[str, Any]) -> str:
    if board_meta.get("archived"):
        return _terminal_worker_reaction_state(worker) or "done"
    return board_thread_state(board)


def _board_thread_reaction_state_for_status(board: str, board_meta: dict[str, Any], worker: dict[str, Any]) -> str:
    if board_meta.get("archived"):
        return _terminal_worker_reaction_state(worker) or "done"
    return board_thread_reaction_state(board)


def _sync_metadata_path(metadata_path: object) -> Path | None:
    if not metadata_path:
        return None
    try:
        path = Path(str(metadata_path)).expanduser().resolve()
    except Exception:
        return None
    try:
        root = kanban_db.boards_root().resolve()
    except Exception:
        return None
    if root not in path.parents:
        return None
    if path.name != "board.json":
        return None
    return path


def _read_thread_sync_metadata(board: str, metadata_path: object = None) -> tuple[dict[str, Any], Path | None]:
    path = _sync_metadata_path(metadata_path)
    if path is not None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return raw, path
        except Exception:
            logger.debug("Failed to read Discord thread sync metadata from %s", path, exc_info=True)
    return kanban_db.read_board_metadata(board), None


def _write_thread_sync_metadata(board: str, metadata: dict[str, Any], path: Path | None) -> None:
    _write_metadata_to_path(board, metadata, path)


def mark_thread_status_synced(
    board: str,
    *,
    reaction: bool = False,
    summary: bool = False,
    completion_message: bool = False,
    metadata_path: object = None,
) -> None:
    """Clear one-shot terminal Discord thread sync flags for a board.

    ``completion_message`` only acknowledges that a pending completion notice no
    longer needs delivery (stale/unsupported targets). Successful sends should
    call :func:`mark_thread_completion_notice_sent` so there is durable proof
    that the user-visible completion message actually went out.
    """
    if not board or not (reaction or summary or completion_message):
        return
    sync_path = _sync_metadata_path(metadata_path)

    def mutate(metadata: dict[str, Any], worker: dict[str, Any]) -> bool:
        if worker.get("kind") != "discord_worker_board":
            return False
        changed = False
        if reaction:
            if worker.pop("terminal_reaction_sync_pending", None) is not None:
                changed = True
            if sync_path is not None:
                reaction_state = _terminal_worker_reaction_state(worker)
            else:
                try:
                    reaction_state = board_thread_reaction_state(board)
                except Exception:
                    reaction_state = ""
            if reaction_state in {"done", "blocked", "errored"} and _terminal_reaction_synced_state(worker) != reaction_state:
                worker["terminal_reaction_synced_state"] = reaction_state
                changed = True
        if summary and worker.pop("terminal_summary_sync_pending", None) is not None:
            changed = True
        if completion_message and worker.pop("terminal_completion_message_pending", None) is not None:
            changed = True
        if changed:
            worker["updated_at"] = _now()
        return changed

    _mutate_worker_metadata(
        board,
        mutate,
        metadata_path=sync_path,
        warning_action="mark Discord thread status synced",
    )


def mark_thread_completion_notice_sent(
    board: str,
    *,
    message_id: Optional[str] = None,
    metadata_path: object = None,
) -> None:
    """Record durable proof that a terminal completion notice was sent."""
    if not board:
        return
    now = _now()
    cleaned_message_id = str(message_id or "").strip()
    sync_path = _sync_metadata_path(metadata_path)

    def mutate(metadata: dict[str, Any], worker: dict[str, Any]) -> bool:
        if worker.get("kind") != "discord_worker_board":
            return False
        changed = worker.pop("terminal_completion_message_pending", None) is not None
        if worker.get("terminal_completion_message_sent_at") != now:
            worker["terminal_completion_message_sent_at"] = now
            changed = True
        if cleaned_message_id and worker.get("terminal_completion_message_id") != cleaned_message_id:
            worker["terminal_completion_message_id"] = cleaned_message_id
            changed = True
        if changed:
            worker["updated_at"] = now
        return changed

    _mutate_worker_metadata(
        board,
        mutate,
        metadata_path=sync_path,
        warning_action="mark Discord thread completion notice sent",
    )


def _board_runtime_snapshot(
    worker: dict[str, Any],
    *,
    counts: dict[str, Any],
    running: list[dict[str, Any]],
    conn: Any,
) -> dict[str, Any]:
    queued_ready = int(counts.get("ready") or 0)
    queued_todo = int(counts.get("todo") or 0)
    queued_review = int(counts.get("review") or 0)
    queued_total = queued_ready + queued_todo + queued_review
    running_count = len(running)
    meta_blocked = bool(str(worker.get("blocked_reason") or "").strip()) or worker.get("goal_status") == "blocked"
    meta_blocked_superseded = _worker_meta_allows_ready_dispatch(worker, counts=counts)

    if worker.get("cancelled") or worker.get("goal_status") == "cancelled":
        state = "cancelled"
        reason = "cancelled"
    elif running_count > 0:
        state = "running"
        reason = _running_status_text(running)
    elif int(counts.get("blocked") or 0) > 0:
        state = "blocked"
        reason = _blocked_runtime_reason(worker, conn=conn)
    elif meta_blocked and not meta_blocked_superseded:
        state = "blocked"
        reason = _blocked_runtime_reason(worker, conn=conn)
    elif int(counts.get("running") or 0) > 0:
        state = "stalled"
        reason = "running ticket has no live worker"
    elif (
        worker.get("paused")
        or worker.get("goal_status") == "paused"
        or worker.get("phase") == "paused"
    ):
        state = "paused"
        reason = str(worker.get("paused_reason") or "queue paused")
    elif worker.get("goal_status") == "done" or worker.get("phase") == "complete":
        state = "done"
        reason = "complete"
    elif queued_total > 0:
        state = "queued"
        reason = _queue_reason(worker, counts=counts, running_count=running_count, conn=conn)
    elif int(counts.get("done") or 0) > 0 and not any(
        int(counts.get(status) or 0)
        for status in ("triage", "todo", "ready", "running", "blocked", "review")
    ):
        state = "done"
        reason = "all tickets done"
    else:
        state = "idle"
        reason = "no active tickets"

    if state == "paused":
        control = "resume"
        control_label = "Resume"
    elif state == "running":
        control = "pause"
        control_label = "Pause"
    elif state == "queued":
        control = "pause"
        control_label = "Pause Queue"
    elif state == "blocked" and _is_review_loop_limit_blocker(worker):
        control = "continue"
        control_label = f"Continue (+{REVIEW_LOOP_CONTINUE_EXTRA_LOOPS} loops)"
    elif state in {"idle"} and worker.get("goal_status") in {"unset", None, ""}:
        control = "start"
        control_label = "Start"
    else:
        control = "none"
        control_label = ""

    return {
        "state": state,
        "reason": reason,
        "running_count": running_count,
        "queued_count": queued_total,
        "control": control,
        "control_label": control_label,
    }


def _blocked_runtime_reason(worker: dict[str, Any], *, conn: Any) -> str:
    reason = _public_runtime_reason(
        worker.get("blocked_reason"),
        max_chars=240,
    )
    if reason:
        return reason
    try:
        tasks = kanban_db.list_tasks(conn, include_archived=False)
    except Exception:
        tasks = []
    for task in tasks:
        if getattr(task, "status", "") != "blocked":
            continue
        detail = _public_runtime_reason(
            getattr(task, "last_failure_error", None) or getattr(task, "title", ""),
            max_chars=240,
        )
        return detail or "blocked ticket needs attention"
    return "blocked"


def _public_runtime_reason(value: Any, *, max_chars: int = 240) -> str:
    text = _clean_feature_summary_text(value, max_chars=max_chars, default="")
    return str(_redact_public_state(text) or "").strip() if text else ""


def _runtime_action_form_html(
    session_id: str,
    runtime: dict[str, Any],
    *,
    return_to: str = "",
) -> str:
    control = str(runtime.get("control") or "none")
    control_label = str(runtime.get("control_label") or "")
    if control in {"resume", "start"}:
        action = "start"
        label = control_label or ("Resume" if control == "resume" else "Start")
    elif control == "continue":
        action = "continue"
        label = control_label or "Continue"
    elif control == "pause":
        action = "pause"
        label = control_label or "Pause"
    else:
        return ""

    action_url = f"/workers/{quote(session_id, safe='')}/{action}"
    if return_to:
        action_url = f"{action_url}?return_to={quote(return_to, safe='/')}"
    return (
        f'<form method="post" action="{html.escape(action_url)}">'
        f'<button type="submit">{html.escape(label)}</button></form>'
    )


def _queue_reason(
    worker: dict[str, Any],
    *,
    counts: dict[str, Any],
    running_count: int,
    conn: Any,
) -> str:
    if worker.get("paused") or worker.get("goal_status") == "paused" or worker.get("phase") == "paused":
        return "queue paused"
    if worker.get("cancelled") or worker.get("goal_status") == "cancelled":
        return "cancelled"
    if worker.get("execution_mode") != "kanban_pipeline" or worker.get("goal_status") != "active":
        return "board is not active"
    if int(counts.get("ready") or 0) <= 0 and int(counts.get("review") or 0) <= 0:
        if int(counts.get("todo") or 0) > 0:
            return "waiting on dependencies"
        return "waiting for dispatchable work"

    cfg = _kanban_config()
    if cfg.get("dispatch_in_gateway") is False:
        return "dispatcher disabled in config"

    worker_cfg = _worker_config()
    try:
        max_per_board = int(worker_cfg.get("max_workers_per_board") or 2)
    except (TypeError, ValueError):
        max_per_board = 2
    try:
        max_dev_per_board = int(worker_cfg.get("max_dev_workers_per_board") or 1)
    except (TypeError, ValueError):
        max_dev_per_board = 1
    try:
        max_global = int(worker_cfg.get("max_global_workers") or 8)
    except (TypeError, ValueError):
        max_global = 8
    if max_per_board > 0 and running_count >= max_per_board:
        return "board worker limit reached"
    if max_dev_per_board > 0:
        try:
            running_dev = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE status = 'running' AND lower(assignee) = ?",
                (ROLE_DEV,),
            ).fetchone()[0]
        except Exception:
            running_dev = 0
        if int(running_dev or 0) >= max_dev_per_board:
            return "dev worker limit reached"
        if max_dev_per_board > 1 and _shared_dev_workspace_limited(conn):
            return "shared worktree dev worker limit reached"
    if max_global > 0:
        try:
            if _active_role_count_across_boards() >= max_global:
                return "global worker limit reached"
        except Exception:
            pass

    if not kanban_db.has_spawnable_ready(conn, additional_spawnable_assignees=ROLE_ASSIGNEES):
        return "ready tickets are assigned to non-spawnable lanes"
    return "awaiting next dispatcher tick"


def _shared_dev_workspace_limited(conn: Any) -> bool:
    try:
        rows = conn.execute(
            "SELECT workspace_path FROM tasks "
            "WHERE status IN ('ready', 'running') AND lower(assignee) = ?",
            (ROLE_DEV,),
        ).fetchall()
    except Exception:
        return False
    paths = [str(row["workspace_path"] or "").strip() for row in rows]
    paths = [path for path in paths if path]
    return len(paths) > 1 and len(set(paths)) == 1


def _running_ticket_snapshot(conn: Any) -> list[dict[str, Any]]:
    running = [
        task for task in kanban_db.list_tasks(conn, include_archived=False)
        if task.status == "running"
    ]
    rows = []
    for task in running:
        run = kanban_db.active_run(conn, task.id)
        if task.worker_pid and not kanban_db._pid_alive(int(task.worker_pid)):
            continue
        if run is None and task.current_run_id is None and not task.worker_pid:
            continue
        rows.append(
            {
                "id": task.id,
                "title": task.title,
                "assignee": task.assignee,
                "worker_pid": task.worker_pid,
                "started_at": task.started_at,
                "last_heartbeat_at": task.last_heartbeat_at,
                "current_run_id": task.current_run_id,
                "run_id": getattr(run, "id", None),
                "run_started_at": getattr(run, "started_at", None),
            }
        )
        if len(rows) >= 5:
            break
    return rows


def _running_status_text(items: list[dict[str, Any]]) -> str:
    if not items:
        return "idle"
    parts = []
    for item in items:
        label = str(item.get("title") or item.get("id") or "ticket")
        assignee = str(item.get("assignee") or "").strip()
        pid = item.get("worker_pid")
        bits = [label]
        if assignee:
            bits.append(f"assignee={assignee}")
        if pid:
            bits.append(f"pid={pid}")
        heartbeat = _format_public_timestamp(item.get("last_heartbeat_at"))
        if heartbeat:
            bits.append(f"heartbeat={heartbeat}")
        parts.append(" (".join([bits[0], ", ".join(bits[1:]) + ")"]) if len(bits) > 1 else bits[0])
    if len(items) >= 5:
        parts.append("...")
    return "; ".join(parts)


def _current_run_state(task: Any, runs: list[Any]) -> Optional[dict[str, Any]]:
    current_run_id = getattr(task, "current_run_id", None)
    if current_run_id is not None:
        for run in runs:
            if getattr(run, "id", None) == current_run_id:
                return _run_state_dict(run)
    for run in reversed(runs):
        if getattr(run, "ended_at", None) is None or getattr(run, "status", "") == "running":
            return _run_state_dict(run)
    if runs:
        return _run_state_dict(runs[-1])
    return None


def _ticket_codex_state(task_id: str, *, board: str) -> dict[str, Any]:
    state = _read_codex_worker_state(task_id, board=board)
    if not state:
        return {
            "available": False,
            "message": "No Codex app-server internals captured for this ticket yet.",
        }
    state = dict(state)
    state["available"] = True
    return state


def render_public_board_html(token: str) -> str:
    snapshot = public_board_snapshot(token)
    return _render_public_board_html(snapshot)


def render_public_session_board_html(
    session_id: str,
    *,
    active_ticket_id: Optional[str] = None,
) -> str:
    snapshot = public_board_snapshot_for_session(session_id)
    return _render_public_board_html(snapshot, active_ticket_id=active_ticket_id)


def _workers_page_css() -> str:
    return """
    :root { color-scheme: light; --bg: #f7f7f5; --panel: #ffffff; --panel-soft: #fbfbfa; --line: #d7d7d2; --line-soft: #e6e6e2; --text: #1f2933; --muted: #52606d; --link: #1d4ed8; --code: #5965f2; --runtime-on-dark: #ffffff; --status-running: #075985; --status-queued: #4338ca; --status-idle: #475569; --status-blocked: #991b1b; --status-stalled: #92400e; --status-degraded: #854d0e; --status-paused: #9a3412; --status-done: #047857; --status-cancelled: #4b5563; }
    * { box-sizing: border-box; }
    body { margin: 0; min-height: 100vh; font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--bg); color: var(--text); }
    header { border-bottom: 1px solid var(--line); background: var(--panel); padding: 24px 28px; }
    .brand { color: var(--link); display: inline-block; font-size: 13px; font-weight: 700; line-height: 1.05; text-decoration: none; }
    .brand:hover { text-decoration: underline; }
    .top-nav { align-items: flex-start; display: flex; gap: 18px; justify-content: space-between; }
    .back-link { color: var(--link); font-size: 14px; font-weight: 700; text-decoration: none; }
    .back-link:hover { text-decoration: underline; }
    .hero { margin-top: 14px; }
    h1 { font-size: 24px; line-height: 1.2; margin: 0 0 8px; text-transform: none; }
    .subtle { color: var(--muted); font-size: 14px; }
    main { padding: 20px; }
    a { color: var(--link); text-decoration: none; }
    a:hover { text-decoration: underline; }
    code { color: var(--code); font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.9em; }
    p { color: var(--muted); font-size: 13px; margin: 8px 0 0; }
    .board-list { list-style: none; margin: 0; max-width: 920px; padding: 0; }
    .board-card, .column, .criteria { background: var(--panel); border: 1px solid var(--line-soft); border-radius: 8px; }
    .board-card { margin-bottom: 14px; padding: 16px 18px; transition: border-color 120ms ease, box-shadow 120ms ease; }
    .board-card:hover { border-color: var(--line); box-shadow: 0 2px 12px rgba(15, 23, 42, 0.06); }
    .board-card-head { display: block; padding: 0; }
    .board-title { font-size: 16px; font-weight: 700; line-height: 1.3; text-decoration: none; }
    .board-title:hover { text-decoration: none; border-bottom: 2px solid var(--link); }
    .board-meta, .meta { color: var(--muted); display: flex; flex-wrap: wrap; gap: 6px 12px; font-size: 13px; margin-top: 8px; }
    .meta span { white-space: nowrap; }
    .chips { margin-top: 8px; }
    .chip { color: var(--muted); font-size: 13px; }
    .runtime { background: var(--status-idle); border-radius: 4px; color: var(--runtime-on-dark); display: inline-block; font-size: 11px; font-weight: 700; letter-spacing: 0.05em; padding: 2px 8px; text-transform: uppercase; }
    .runtime-running { background: var(--status-running); color: var(--runtime-on-dark); }
    .runtime-queued { background: var(--status-queued); color: var(--runtime-on-dark); }
    .runtime-idle { background: var(--status-idle); color: var(--runtime-on-dark); }
    .runtime-blocked { background: var(--status-blocked); color: var(--runtime-on-dark); }
    .runtime-stalled { background: var(--status-stalled); color: var(--runtime-on-dark); }
    .runtime-degraded { background: var(--status-degraded); color: var(--runtime-on-dark); }
    .runtime-paused { background: var(--status-paused); color: var(--runtime-on-dark); }
    .runtime-done { background: var(--status-done); color: var(--runtime-on-dark); }
    .runtime-cancelled { background: var(--status-cancelled); color: var(--runtime-on-dark); }
    .board-card-body { display: block; padding: 0; }
    .status-grid { color: var(--muted); font-size: 13px; margin-top: 8px; }
    .status-cell { display: inline; }
    .status-cell + .status-cell::before { content: " "; }
    .status-cell b { font-weight: 400; }
    .status-cell span::after { content: ":"; }
    .status-cell span { margin-right: 0; }
    .reason { color: var(--muted); font-size: 13px; margin-top: 8px; }
    .actions { margin-top: 12px; }
    form { display: inline; margin: 0; }
    button, .button-link { background: var(--text); border: 1px solid var(--text); border-radius: 6px; color: #ffffff; cursor: pointer; font: inherit; padding: 6px 12px; text-decoration: none; transition: background 120ms ease; }
    button:hover, .button-link:hover { background: #374151; }
    .board { display: grid; gap: 14px; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); }
    .column { min-height: 200px; overflow: hidden; transition: border-color 120ms ease, box-shadow 120ms ease; }
    .column.column-drop { border-color: var(--code); box-shadow: 0 0 0 2px rgba(89, 101, 242, 0.16); }
    .column.column-disabled { opacity: 0.72; }
    .column h2 { align-items: center; border-bottom: 1px solid var(--line-soft); display: flex; font-size: 13px; justify-content: space-between; margin: 0; padding: 12px 14px; text-transform: uppercase; letter-spacing: 0.04em; }
    .column h2 span[data-column-count] { background: var(--line-soft); border-radius: 99px; color: var(--muted); font-size: 11px; font-weight: 700; min-width: 20px; padding: 1px 7px; text-align: center; }
    .column ul { list-style: none; margin: 0; min-height: 140px; padding: 10px; }
    .column li { background: var(--panel-soft); border: 1px solid var(--line-soft); border-radius: 6px; margin-bottom: 8px; padding: 10px 12px; transition: border-color 100ms ease; }
    .column li:hover { border-color: var(--line); }
    .column-empty { align-items: center; color: var(--muted); display: flex; font-size: 13px; font-style: italic; justify-content: center; min-height: 140px; opacity: 0.6; padding: 24px; text-align: center; }
    .ticket-card { cursor: grab; touch-action: manipulation; }
    .ticket-card.dragging { opacity: 0.48; }
    .ticket-card:active { cursor: grabbing; }
    .ticket-shell { align-items: flex-start; display: grid; gap: 8px; grid-template-columns: minmax(0, 1fr) auto; }
    .ticket { appearance: none; background: transparent; border: 0; color: inherit; cursor: pointer; display: block; font: inherit; padding: 0; text-align: left; width: 100%; }
    .ticket:hover strong { color: var(--link); text-decoration: underline; }
    .ticket:focus-visible { outline: 2px solid var(--code); outline-offset: 3px; }
    .ticket strong { display: block; font-size: 14px; line-height: 1.3; }
    .ticket > div { margin-top: 2px; }
    .ticket p { color: var(--muted); font-size: 13px; margin: 6px 0 0; }
    .ticket-brief, .ticket-summary { margin: 4px 0 0; }
    .ticket-console { align-items: center; background: #111827; border: 1px solid #374151; border-radius: 6px; color: #f9fafb; display: inline-flex; font: 700 12px/1 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; justify-content: center; min-height: 28px; min-width: 32px; padding: 6px 7px; text-decoration: none; }
    .ticket-console:hover { background: #1f2937; color: #ffffff; text-decoration: none; }
    .ticket-console:focus-visible { outline: 2px solid var(--code); outline-offset: 2px; }
    .move-error { background: #fef2f2; border: 1px solid #fecaca; border-radius: 8px; color: #991b1b; font-size: 13px; margin-bottom: 12px; padding: 10px 14px; }
    .move-error[hidden] { display: none; }
    .criteria { margin-bottom: 18px; padding: 16px; }
    .criteria strong { display: block; font-size: 14px; font-weight: 700; margin-bottom: 10px; }
    .criteria ol { margin: 0; padding-left: 20px; }
    .criteria li { color: var(--text); margin: 6px 0; }
    .board-summary pre { color: var(--text); font: 12px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; margin: 0; white-space: pre-wrap; word-break: break-word; }
    .empty-state { align-items: center; color: var(--muted); display: flex; flex-direction: column; gap: 8px; justify-content: center; padding: 64px 24px; text-align: center; }
    .empty-state h2 { color: var(--muted); font-size: 18px; font-weight: 700; margin: 0; }
    .empty-state p { font-size: 14px; margin: 0; max-width: 420px; }
    .degraded-banner { background: #fffbeb; border: 1px solid #fcd34d; border-radius: 8px; color: #92400e; font-size: 13px; margin-bottom: 14px; padding: 10px 14px; }
    .modal { align-items: center; background: rgba(15, 23, 42, 0.54); display: none; inset: 0; justify-content: center; padding: 20px; position: fixed; z-index: 20; }
    .modal[aria-hidden="false"] { display: flex; }
    .modal-panel { background: #ffffff; border: 1px solid var(--line); border-radius: 12px; box-shadow: 0 24px 60px rgba(15, 23, 42, 0.28); max-height: min(86vh, 900px); max-width: min(920px, 96vw); min-width: min(720px, 96vw); overflow: hidden; }
    .modal-head { align-items: center; border-bottom: 1px solid var(--line-soft); display: flex; gap: 16px; justify-content: space-between; padding: 14px 18px; }
    .modal-head h2 { font-size: 16px; margin: 0; }
    .modal-close { background: #f3f4f6; border: 1px solid var(--line); color: var(--text); }
    .modal-close:hover { background: #e5e7eb; }
    .modal-body { background: #111827; color: #f9fafb; font: 13px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; margin: 0; max-height: calc(min(86vh, 900px) - 58px); overflow: auto; padding: 16px 18px; white-space: pre-wrap; word-break: break-word; }
    @media (max-width: 760px) { .modal-panel { min-width: min(100%, 96vw); } .board { grid-template-columns: 1fr; } }
    """


def _render_public_board_html(
    snapshot: dict[str, Any],
    *,
    active_ticket_id: Optional[str] = None,
) -> str:
    worker = snapshot["worker"]
    tasks = snapshot["tasks"]
    runtime = snapshot.get("runtime") or {"state": "idle", "reason": "no active tickets"}
    session_id = str(snapshot.get("session_id") or worker.get("thread_id") or "")
    columns = list(PUBLIC_BOARD_COLUMNS)
    by_status = {status: [] for status in columns}
    for task in tasks:
        by_status.setdefault(task["status"], []).append(task)

    def esc(value: Any) -> str:
        return html.escape(str(value or ""))

    board_url = f"/workers/{quote(session_id, safe='')}"
    board_url_json = json.dumps(board_url).replace("</", "<\\/")
    active_ticket_json = json.dumps(str(active_ticket_id or "")).replace("</", "<\\/")

    def render_ticket_card(item: dict[str, Any], status: str) -> str:
        ticket_id = str(item["id"])
        quoted_ticket = quote(ticket_id, safe="")
        ticket_url = f"{board_url}/tickets/{quoted_ticket}"
        body_preview = str(item.get("body_preview") or "").strip()
        summary = str(item.get("latest_summary") or "").strip()
        brief_html = (
            f'<p class="ticket-brief"><b>Brief:</b> {esc(body_preview)}</p>'
            if body_preview else ""
        )
        summary_html = (
            f'<p class="ticket-summary"><b>Latest:</b> {esc(summary)}</p>'
            if summary else ""
        )
        assignee_raw = str(item.get("assignee") or "").strip()
        assignee_html = f' <span class="chip">[{esc(assignee_raw)}]</span>' if assignee_raw else ""
        return (
            "<li class=\"ticket-card\" draggable=\"true\" data-ticket-item "
            "data-ticket-id=\"{id}\" data-ticket-status=\"{status}\" "
            "data-ticket-move-url=\"{move_url}\">"
            "<div class=\"ticket-shell\">"
            "<button type=\"button\" class=\"ticket\" data-ticket-id=\"{id}\" "
            "data-ticket-title=\"{title}\" data-ticket-url=\"{ticket_url}\" "
            "data-ticket-state-url=\"{state_url}\" "
            "data-ticket-terminal-page-url=\"{terminal_page_url}\" "
            "data-ticket-terminal-url=\"{terminal_url}\">"
            "<strong>{title}</strong>"
            "<div><code>{id}</code>{assignee}</div>{brief}{summary}"
            "</button>"
            "<a class=\"ticket-console\" href=\"{console_url}\" "
            "data-ticket-console-url=\"{console_url}\" "
            "title=\"Open worker console\" aria-label=\"Open worker console for {title}\">"
            "<span aria-hidden=\"true\">&gt;_</span>"
            "</a></div></li>".format(
                title=esc(item["title"]),
                id=esc(ticket_id),
                status=esc(status),
                ticket_url=esc(ticket_url),
                state_url=esc(f"{ticket_url}/state"),
                terminal_page_url=esc(f"{ticket_url}/terminal"),
                terminal_url=esc(f"{ticket_url}/terminal.json"),
                console_url=esc(f"{ticket_url}/console"),
                move_url=esc(f"{ticket_url}/move"),
                assignee=assignee_html,
                brief=brief_html,
                summary=summary_html,
            )
        )

    cards = []
    for status in columns:
        items = by_status.get(status, [])
        body = "\n".join(render_ticket_card(item, status) for item in items)
        column_body = f"<ul data-ticket-list>{body}</ul>" if items else (
            f'<ul data-ticket-list class="column-empty">No {esc(status)} tickets</ul>'
        )
        disabled = " column-disabled" if status == "running" else ""
        drop_disabled = "true" if status == "running" else "false"
        cards.append(
            f"<section class=\"column{disabled}\" data-column data-status=\"{esc(status)}\" "
            f"data-drop-disabled=\"{drop_disabled}\"><h2>{esc(status)} "
            f"<span data-column-count>{len(items)}</span></h2>"
            f"{column_body}</section>"
        )
    criteria = "\n".join(
        f"<li>{esc(c.get('text') if isinstance(c, dict) else c)}</li>"
        for c in (worker.get("criteria") or [])
        if (c.get("active", True) if isinstance(c, dict) else True)
    )
    discord_thread_url = str(worker.get("discord_thread_url") or "").strip()
    thread_display = str(worker.get("thread_id") or session_id)
    session_text = (
        f'<a href="{esc(discord_thread_url)}" target="_blank" rel="noopener noreferrer">'
        f"<code>{esc(thread_display)}</code></a>"
        if discord_thread_url
        else f"<code>{esc(thread_display)}</code>"
    )
    runtime_action = _runtime_action_form_html(
        session_id,
        runtime,
        return_to=board_url,
    )
    summary_html = _public_board_summary_html(snapshot.get("board_summary") or {})
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc(snapshot["name"])}</title>
  <style>
{_workers_page_css()}
  </style>
</head>
<body>
  <header>
    <div class="top-nav">
      <a class="brand" href="/command-center">Command<br>Center</a>
      <a class="back-link" href="/workers">Worker Boards</a>
    </div>
    <div class="hero">
      <div>
        <h1>{esc(snapshot["name"])}</h1>
        <div class="meta">
          <span>Discord: {session_text}</span>
          <span>Status: {esc(_public_status_text(worker))}</span>
          <span>Branch: {esc(worker.get("worker_branch") or "")}</span>
          <span>PR: {esc(worker.get("pr_url") or "not opened")}</span>
          <span>Review: {esc(_public_review_text(worker))}</span>
          <span>Updated: {esc(_format_public_timestamp(worker.get("updated_at")) or "never")}</span>
          <span>Runtime: <strong class="runtime runtime-{esc(runtime.get("state"))}">{esc(runtime.get("state"))}</strong></span>
          <span>{esc(runtime.get("reason"))}</span>
        </div>
        <div class="actions">{runtime_action}</div>
      </div>
    </div>
  </header>
  <main>
    {summary_html}
    <div class="criteria"><strong>Acceptance Criteria</strong><ol>{criteria}</ol></div>
    <div class="move-error" id="ticket-move-error" role="alert" hidden></div>
    <div class="board">{''.join(cards)}</div>
  </main>
  <div class="modal" id="ticket-modal" aria-hidden="true" role="dialog" aria-modal="true" aria-labelledby="ticket-modal-title">
    <div class="modal-panel">
      <div class="modal-head">
        <h2 id="ticket-modal-title">Ticket State</h2>
        <button class="modal-close" type="button" id="ticket-modal-close">Close</button>
      </div>
      <pre class="modal-body" id="ticket-modal-body">Loading...</pre>
    </div>
  </div>
  <script>
    (() => {{
      const modal = document.getElementById("ticket-modal");
      const title = document.getElementById("ticket-modal-title");
      const body = document.getElementById("ticket-modal-body");
      const close = document.getElementById("ticket-modal-close");
      const moveError = document.getElementById("ticket-move-error");
      let draggingItem = null;
      let touchState = null;
      let suppressClickUntil = 0;
      const boardUrl = {board_url_json};
      const initialTicketId = {active_ticket_json};
      const columns = Array.from(document.querySelectorAll("[data-column]"));
      const ticketUrlFor = (ticketId) => boardUrl + "/tickets/" + encodeURIComponent(ticketId || "");
      const stateUrlFor = (ticketId) => ticketUrlFor(ticketId) + "/state";
      const terminalPageUrlFor = (ticketId) => ticketUrlFor(ticketId) + "/terminal";
      const ticketIdFromPath = () => {{
        const prefix = boardUrl + "/tickets/";
        if (!window.location.pathname.startsWith(prefix)) return "";
        const encoded = window.location.pathname.slice(prefix.length).split("/")[0];
        if (!encoded) return "";
        try {{ return decodeURIComponent(encoded); }} catch (_error) {{ return encoded; }}
      }};
      const labelForTicket = (ticketId) => {{
        for (const button of document.querySelectorAll("[data-ticket-state-url]")) {{
          if (button.dataset.ticketId === ticketId) return button.dataset.ticketTitle || ticketId;
        }}
        return ticketId || "Ticket State";
      }};
      const setPageUrl = (url, replace) => {{
        if (!url || !window.history || typeof window.history.pushState !== "function") return;
        if (window.location.pathname === url && !window.location.search && !window.location.hash) return;
        const state = url === boardUrl ? {{}} : {{ ticketId: url.slice(url.lastIndexOf("/") + 1) }};
        if (replace && typeof window.history.replaceState === "function") window.history.replaceState(state, "", url);
        else window.history.pushState(state, "", url);
      }};
      const clearColumnDrops = () => {{
        columns.forEach((column) => column.classList.remove("column-drop"));
      }};
      const showMoveError = (message) => {{
        if (!moveError) return;
        moveError.textContent = message || "Move failed";
        moveError.hidden = false;
      }};
      const clearMoveError = () => {{
        if (!moveError) return;
        moveError.textContent = "";
        moveError.hidden = true;
      }};
      const updateCounts = () => {{
        columns.forEach((column) => {{
          const count = column.querySelector("[data-column-count]");
          const list = column.querySelector("[data-ticket-list]");
          if (count && list) count.textContent = String(list.querySelectorAll("[data-ticket-item]").length);
        }});
      }};
      const parseApiError = async (response) => {{
        try {{
          const payload = await response.json();
          return payload?.detail || `HTTP ${{response.status}}`;
        }} catch (_error) {{
          return `HTTP ${{response.status}}`;
        }}
      }};
      const moveTicket = async (item, targetColumn) => {{
        if (!item || !targetColumn) return;
        const status = targetColumn.dataset.status || "";
        if (targetColumn.dataset.dropDisabled === "true") {{
          showMoveError("Workers can only enter running through the dispatcher.");
          return;
        }}
        if (!status || item.dataset.ticketStatus === status) return;
        const targetList = targetColumn.querySelector("[data-ticket-list]");
        if (!targetList) return;

        clearMoveError();
        const originalParent = item.parentElement;
        const originalNext = item.nextSibling;
        const originalStatus = item.dataset.ticketStatus || "";
        targetList.appendChild(item);
        item.dataset.ticketStatus = status;
        updateCounts();
        try {{
          const response = await fetch(item.dataset.ticketMoveUrl, {{
            method: "POST",
            headers: {{
              "Accept": "application/json",
              "Content-Type": "application/json",
            }},
            body: JSON.stringify({{ status }}),
          }});
          if (!response.ok) {{
            throw new Error(await parseApiError(response));
          }}
          window.location.reload();
        }} catch (error) {{
          if (originalParent) originalParent.insertBefore(item, originalNext);
          item.dataset.ticketStatus = originalStatus;
          updateCounts();
          showMoveError(`Move failed: ${{error?.message || error}}`);
        }}
      }};
      const hide = (options = {{}}) => {{
        modal.setAttribute("aria-hidden", "true");
        body.textContent = "";
        if (options.updateUrl !== false) setPageUrl(boardUrl, options.replaceUrl === true);
      }};
      const show = (label) => {{
        title.textContent = label ? label + " - Details" : "Ticket Details";
        body.textContent = "Loading...";
        modal.setAttribute("aria-hidden", "false");
      }};
      const renderTicketState = (state, terminalPageUrl) => {{
        const task = state?.task || {{}};
        const workerRun = state?.worker_run || null;
        const currentRun = state?.current_run || null;
        const runs = Array.isArray(state?.runs) ? state.runs : [];
        const latestRun = runs.length ? runs[runs.length - 1] : null;
        const lines = [
          `$ ticket ${{task.id || ""}}`,
          `title: ${{task.title || ""}}`,
          `status: ${{task.status || "unknown"}}`,
          `assignee: ${{task.assignee || "unassigned"}}`,
          `terminal: ${{terminalPageUrl || ""}}`,
          "",
          "# brief",
          task.body || "(no ticket body provided)",
        ];
        if (workerRun?.summary) {{
          lines.splice(4, 0, `worker_run: ${{workerRun.summary}}`);
        }}
        if (currentRun) {{
          lines.push("", "# current run");
          lines.push(`run=${{currentRun.id || "-"}} status=${{currentRun.status || currentRun.outcome || "-"}}`);
        }}
        if (latestRun?.summary) {{
          lines.push("", "# latest worker summary", latestRun.summary);
        }}
        if (task.result) {{
          lines.push("", "# result", task.result);
        }}
        if (task.last_failure_error) {{
          lines.push("", "# last failure", task.last_failure_error);
        }}
        return lines.join("\\n");
      }};
      const loadTicketState = async (url, terminalPageUrl) => {{
        const response = await fetch(url, {{ headers: {{ "Accept": "application/json" }} }});
        if (!response.ok) {{
          throw new Error(`HTTP ${{response.status}}`);
        }}
        const state = await response.json();
        body.textContent = renderTicketState(state, terminalPageUrl);
        body.scrollTop = 0;
      }};
      const openTicket = async (ticketId, label, options = {{}}) => {{
        const cleanTicketId = ticketId || "";
        if (!cleanTicketId) return;
        const ticketUrl = options.ticketUrl || ticketUrlFor(cleanTicketId);
        show(label || labelForTicket(cleanTicketId));
        if (options.updateUrl !== false) setPageUrl(ticketUrl, options.replaceUrl === true);
        try {{
          await loadTicketState(
            options.stateUrl || stateUrlFor(cleanTicketId),
            options.terminalPageUrl || terminalPageUrlFor(cleanTicketId),
          );
        }} catch (error) {{
          body.textContent = `Unable to load ticket details: ${{error}}`;
        }}
      }};
      document.querySelectorAll("[data-ticket-item]").forEach((item) => {{
        item.addEventListener("dragstart", (event) => {{
          draggingItem = item;
          item.classList.add("dragging");
          if (event.dataTransfer) {{
            event.dataTransfer.effectAllowed = "move";
            event.dataTransfer.setData("text/plain", item.dataset.ticketId || "");
          }}
        }});
        item.addEventListener("dragend", () => {{
          item.classList.remove("dragging");
          draggingItem = null;
          clearColumnDrops();
        }});
        item.addEventListener("pointerdown", (event) => {{
          if (event.button !== 0) return;
          touchState = {{
            item,
            pointerId: event.pointerId,
            startX: event.clientX,
            startY: event.clientY,
            targetColumn: null,
            started: false,
          }};
          if (event.pointerType !== "mouse" && item.setPointerCapture) item.setPointerCapture(event.pointerId);
        }});
      }});
      columns.forEach((column) => {{
        column.addEventListener("dragover", (event) => {{
          if (!draggingItem || column.dataset.dropDisabled === "true") return;
          event.preventDefault();
          column.classList.add("column-drop");
          if (event.dataTransfer) event.dataTransfer.dropEffect = "move";
        }});
        column.addEventListener("dragleave", (event) => {{
          if (!column.contains(event.relatedTarget)) column.classList.remove("column-drop");
        }});
        column.addEventListener("drop", (event) => {{
          if (!draggingItem) return;
          event.preventDefault();
          clearColumnDrops();
          moveTicket(draggingItem, column);
        }});
      }});
      document.addEventListener("pointermove", (event) => {{
        if (!touchState || event.pointerId !== touchState.pointerId) return;
        const dx = Math.abs(event.clientX - touchState.startX);
        const dy = Math.abs(event.clientY - touchState.startY);
        if (!touchState.started && dx + dy < 10) return;
        touchState.started = true;
        draggingItem = touchState.item;
        touchState.item.classList.add("dragging");
        event.preventDefault();
        const target = document.elementFromPoint(event.clientX, event.clientY)?.closest("[data-column]");
        clearColumnDrops();
        if (target && target.dataset.dropDisabled !== "true") {{
          target.classList.add("column-drop");
          touchState.targetColumn = target;
        }} else {{
          touchState.targetColumn = null;
        }}
      }}, {{ passive: false }});
      document.addEventListener("pointerup", (event) => {{
        if (!touchState || event.pointerId !== touchState.pointerId) return;
        const state = touchState;
        touchState = null;
        clearColumnDrops();
        state.item.classList.remove("dragging");
        draggingItem = null;
        if (state.started) {{
          suppressClickUntil = Date.now() + 350;
          moveTicket(state.item, state.targetColumn);
        }}
      }});
      document.addEventListener("pointercancel", () => {{
        if (touchState?.item) touchState.item.classList.remove("dragging");
        touchState = null;
        draggingItem = null;
        clearColumnDrops();
      }});
      document.addEventListener("click", (event) => {{
        if (Date.now() < suppressClickUntil && event.target.closest("[data-ticket-item]")) {{
          event.preventDefault();
          event.stopPropagation();
        }}
      }}, true);
      document.querySelectorAll("[data-ticket-state-url]").forEach((button) => {{
        button.addEventListener("click", () => {{
          openTicket(
            button.dataset.ticketId || "",
            button.dataset.ticketTitle || button.dataset.ticketId || "Ticket State",
            {{
              ticketUrl: button.dataset.ticketUrl,
              stateUrl: button.dataset.ticketStateUrl,
              terminalPageUrl: button.dataset.ticketTerminalPageUrl,
            }},
          );
        }});
      }});
      close.addEventListener("click", hide);
      modal.addEventListener("click", (event) => {{
        if (event.target === modal) hide();
      }});
      document.addEventListener("keydown", (event) => {{
        if (event.key === "Escape") hide();
      }});
      window.addEventListener("popstate", () => {{
        const ticketId = ticketIdFromPath();
        if (ticketId) openTicket(ticketId, labelForTicket(ticketId), {{ updateUrl: false }});
        else hide({{ updateUrl: false }});
      }});
      const startupTicketId = initialTicketId || ticketIdFromPath();
      if (startupTicketId) openTicket(startupTicketId, labelForTicket(startupTicketId), {{ updateUrl: false }});
    }})();
  </script>
</body>
</html>"""


def public_board_index_snapshot() -> dict[str, Any]:
    def newest_sort_key(item: dict[str, Any]) -> tuple[int, int, str]:
        worker = item.get("worker") or {}
        try:
            created_at = int(worker.get("created_at") or 0)
        except (TypeError, ValueError):
            created_at = 0
        try:
            session_id = int(item.get("session_id") or 0)
        except (TypeError, ValueError):
            session_id = 0
        return (created_at, session_id, str(item.get("board") or ""))

    boards = []
    for board in kanban_db.list_boards(include_archived=False):
        slug = str(board.get("slug") or kanban_db.DEFAULT_BOARD)
        try:
            worker = _read_worker_meta(slug)
            if worker.get("kind") != "discord_worker_board":
                continue
            state = kanban_db.corrupt_board_quarantine_state(slug)
            if state.get("skipped"):
                incident = state.get("incident") if isinstance(state.get("incident"), dict) else {}
                session_id = _public_session_id_for_board(slug, worker)
                boards.append(
                    {
                        "board": slug,
                        "name": _worker_board_name(worker, board, slug),
                        "description": board.get("description") or "",
                        "session_id": session_id,
                        "public_url": public_session_board_url(session_id),
                        "worker": _public_worker_meta(worker),
                        "counts": {},
                        "running": [],
                        "runtime": {
                            "state": "degraded",
                            "reason": "kanban DB corruption quarantine active",
                        },
                        "corruption": {
                            "status": "degraded",
                            "reason": state.get("reason") or incident.get("reason"),
                            "db_path": state.get("db_path") or incident.get("db_path"),
                            "first_seen": incident.get("first_seen"),
                            "last_seen": incident.get("last_seen"),
                            "next_retry": state.get("next_retry") or incident.get("next_retry"),
                            "quarantine_path": incident.get("quarantine_path"),
                        },
                    }
                )
                continue
            conn = kanban_db.connect(board=slug)
            try:
                counts = kanban_db.board_stats(conn).get("by_status", {})
                running = _running_ticket_snapshot(conn)
                runtime = _board_runtime_snapshot(
                    worker,
                    counts=counts,
                    running=running,
                    conn=conn,
                )
            finally:
                conn.close()
            session_id = _public_session_id_for_board(slug, worker)
            boards.append(
                {
                    "board": slug,
                    "name": _worker_board_name(worker, board, slug),
                    "description": board.get("description") or "",
                    "session_id": session_id,
                    "public_url": public_session_board_url(session_id),
                    "worker": _public_worker_meta(worker),
                    "counts": counts,
                    "running": running,
                    "runtime": runtime,
                }
            )
        except Exception as exc:
            logger.warning(
                "Skipping public worker-board index entry for board %s: %s",
                slug,
                type(exc).__name__,
            )
    boards.sort(key=newest_sort_key, reverse=True)
    return {"boards": boards}


def render_public_board_index_html() -> str:
    snapshot = public_board_index_snapshot()

    def esc(value: Any) -> str:
        return html.escape(str(value or ""))

    items = []
    for board in snapshot["boards"]:
        worker = board.get("worker") or {}
        session_id = str(board.get("session_id") or "")
        href = f"/workers/{quote(session_id, safe='')}" if session_id else ""
        counts = board.get("counts") or {}
        running = board.get("running") if isinstance(board.get("running"), list) else []
        runtime = board.get("runtime") if isinstance(board.get("runtime"), dict) else {}
        count_text = _public_count_text(counts)
        running_text = _running_status_text(running)
        runtime_state = str(runtime.get("state") or "idle")
        runtime_reason = str(runtime.get("reason") or running_text)
        runtime_label = "Queue" if runtime_state == "queued" else "Status"
        title = esc(_worker_index_title(worker, board.get("name")))
        link = f'<a class="board-title" href="{esc(href)}">{title}</a>' if href else title
        discord_thread_url = str(worker.get("discord_thread_url") or "").strip()
        thread_display = str(worker.get("thread_id") or session_id)
        session_text = (
            f'<a href="{esc(discord_thread_url)}" target="_blank" rel="noopener noreferrer">'
            f"<code>{esc(thread_display)}</code></a>"
            if discord_thread_url
            else f"<code>{esc(thread_display)}</code>"
        )
        status = _public_status_text(worker)
        execution_mode = str(worker.get("execution_mode") or "").strip()
        branch = str(worker.get("worker_branch") or "").strip()
        pr_url = str(worker.get("pr_url") or "").strip()
        pr_number = str(worker.get("pr_number") or "").strip()
        if pr_url.startswith(("http://", "https://")):
            pr_label = f"#{pr_number}" if pr_number else pr_url
            pr_text = f'<a href="{esc(pr_url)}">{esc(pr_label)}</a>'
        else:
            pr_text = esc(pr_url or "not opened")
        created_at = _format_public_timestamp(worker.get("created_at"))
        updated_at = _format_public_timestamp(worker.get("updated_at"))
        timestamps = " ".join(
            bit for bit in (
                f"Created: {esc(created_at)}" if created_at else "",
                f"Updated: {esc(updated_at)}" if updated_at else "",
            )
            if bit
        )
        flags = []
        if worker.get("paused"):
            flags.append("paused")
        if worker.get("cancelled"):
            flags.append("cancelled")
        blocked_reason = str(worker.get("blocked_reason") or "").strip()
        if blocked_reason:
            flags.append(f"blocked: {blocked_reason}")
        flags_text = " ".join(flags)
        primary_action = _runtime_action_form_html(session_id, runtime)
        corruption = board.get("corruption") if isinstance(board.get("corruption"), dict) else {}
        corruption_banner = ""
        if corruption.get("status") == "degraded":
            corruption_reason = str(corruption.get("reason") or "").strip()
            corruption_banner = (
                f'<div class="degraded-banner">Board data is degraded'
                f'{" — " + esc(corruption_reason) if corruption_reason else ""}.'
                f' Some details may be unavailable.</div>'
            )
        items.append(
            '<li class="board-card">'
            '{corruption_banner}'
            '<strong>{link}</strong><br>'
            '<div class="meta">{session} · Status: {status}</div>'
            '<p>Runtime: <strong class="runtime runtime-{runtime_class}">{runtime}</strong></p>'
            '<p>Tasks: {counts}</p>'
            '<p>{reason_label}: {reason}</p>'
            '<p>Running: {running}</p>'
            '<p>{branch}{mode}{pr}{review}</p>'
            '<p>{timestamps}</p>'
            '{flags}'
            '<div class="actions">{action}</div>'
            '</li>'.format(
                link=link,
                session=session_text,
                status=esc(status),
                runtime=esc(runtime_state),
                runtime_class=esc(runtime_state),
                reason_label=esc(runtime_label),
                reason=esc(runtime_reason),
                counts=esc(count_text),
                running=esc(running_text),
                branch=f"Branch: {esc(branch)}" if branch else "Branch: pending",
                mode=f" Mode: {esc(execution_mode)}" if execution_mode else "",
                pr=f" PR: {pr_text}",
                review=f" Review: {esc(_public_review_text(worker))}",
                timestamps=timestamps,
                flags=f"<p>{esc(flags_text)}</p>" if flags_text else "",
                action=primary_action,
                corruption_banner=corruption_banner,
            )
        )
    body = "\n".join(items) or (
        '<li class="empty-state">'
        '<h2>No public worker boards</h2>'
        '<p>Discord worker boards will appear here once they are created.</p>'
        '</li>'
    )
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Hermes Kanban</title>
  <style>
{_workers_page_css()}
  </style>
</head>
<body>
  <header>
    <a class="brand" href="/">Hermes<br>Kanban</a>
    <div class="hero">
      <h1>Worker Boards</h1>
      <div class="subtle">{len(snapshot["boards"])} public session boards</div>
    </div>
  </header>
  <main>
    <ul class="board-list">{body}</ul>
  </main>
</body>
</html>"""


def set_goal(
    *,
    thread_id: str,
    goal: str,
    chat_id: Optional[str] = None,
    guild_id: Optional[str] = None,
    parent_channel_id: Optional[str] = None,
    project_context: Optional[dict[str, Any]] = None,
    request_id: Optional[str] = None,
    thread_context: Optional[str] = None,
    board_slug: Optional[str] = None,
) -> DiscordBoard:
    return start_planner_request(
        thread_id=thread_id,
        request=goal,
        chat_id=chat_id,
        guild_id=guild_id,
        parent_channel_id=parent_channel_id,
        project_context=project_context,
        request_id=request_id,
        thread_context=thread_context,
        board_slug=board_slug,
        created_by="discord-goal",
    )


def start_direct_goal(
    *,
    thread_id: str,
    goal: str,
    chat_id: Optional[str] = None,
    guild_id: Optional[str] = None,
    parent_channel_id: Optional[str] = None,
    project_context: Optional[dict[str, Any]] = None,
    request_id: Optional[str] = None,
    board_slug: Optional[str] = None,
) -> DiscordBoard:
    """Activate a Discord worker board whose work items already exist."""
    raw_goal = str(goal or "").strip()
    board = ensure_discord_thread_board(
        thread_id=thread_id,
        chat_id=chat_id,
        guild_id=guild_id,
        parent_channel_id=parent_channel_id,
        initial_request=raw_goal,
        project_context=project_context,
        request_id=request_id,
        board_slug=board_slug,
    )
    worker = board.worker
    previous_goal = str(worker.get("root_goal") or worker.get("initial_request") or "")
    if _planner_request_fingerprint(previous_goal) != _planner_request_fingerprint(raw_goal):
        _clear_generated_summary_title(worker)
    _clear_board_run_summary(board.slug, worker)
    _clear_pr_summary_fields(worker)
    worker.update(
        {
            "root_goal": raw_goal,
            "goal_status": "active",
            "phase": "dev",
            "execution_mode": "kanban_pipeline",
            **pr_policy_for_request(raw_goal),
            "paused": False,
            "cancelled": False,
            "terminal_summary_sync_pending": True,
        }
    )
    metadata = _update_worker_meta(board.slug, worker)
    return DiscordBoard(slug=board.slug, metadata=metadata)


def start_planner_request(
    *,
    thread_id: str,
    request: str,
    chat_id: Optional[str] = None,
    guild_id: Optional[str] = None,
    parent_channel_id: Optional[str] = None,
    project_context: Optional[dict[str, Any]] = None,
    request_id: Optional[str] = None,
    thread_context: Optional[str] = None,
    created_by: str = "discord-feature-request",
    board_slug: Optional[str] = None,
    acceptance_criteria: Optional[list[Any]] = None,
) -> DiscordBoard:
    raw_request = _canonical_planner_request_text(request)
    board = ensure_discord_thread_board(
        thread_id=thread_id,
        chat_id=chat_id,
        guild_id=guild_id,
        parent_channel_id=parent_channel_id,
        initial_request=raw_request,
        project_context=project_context,
        request_id=request_id,
        board_slug=board_slug,
        source_message_id=request_id,
    )
    worker = board.worker
    previous_goal_status = str(worker.get("goal_status") or "").strip().lower()
    starts_new_goal_run = previous_goal_status in TERMINAL_GOAL_STATUSES
    previous_goal = str(
        worker.get("latest_planner_request")
        or worker.get("root_goal")
        or worker.get("initial_request")
        or ""
    )
    request_changed = _planner_request_fingerprint(previous_goal) != _planner_request_fingerprint(raw_request)
    planner_key = _planner_request_key(
        raw_request,
        request_id=request_id,
        include_request_id=starts_new_goal_run,
    )
    thread_context_text = _merge_expanded_discord_thread_context(
        raw_request,
        str(thread_context or ""),
    )
    artifact_worker = dict(worker)
    if acceptance_criteria is not None:
        artifact_worker["criteria"] = acceptance_criteria
    if thread_context_text:
        artifact_worker["latest_goal_thread_context"] = thread_context_text
    plan_artifacts = _discord_plan_artifact_context(raw_request, artifact_worker)
    if request_changed:
        _clear_generated_summary_title(worker)
    if starts_new_goal_run:
        _clear_board_run_summary(board.slug, worker)
        _clear_pr_summary_fields(worker)
    worker.update(
        {
            "root_goal": raw_request,
            "latest_planner_request": raw_request,
            "latest_planner_request_key": planner_key,
            "goal_status": "active",
            "phase": "planning",
            "execution_mode": "kanban_pipeline",
            **pr_policy_for_request(raw_request),
            "paused": False,
            "cancelled": False,
            "terminal_summary_sync_pending": True,
        }
    )
    if acceptance_criteria is not None:
        worker["criteria"] = acceptance_criteria
        if acceptance_criteria:
            worker["criteria_source"] = "explicit"
        else:
            worker.pop("criteria_source", None)
    if thread_context_text:
        worker["latest_goal_thread_context"] = thread_context_text
    else:
        worker.pop("latest_goal_thread_context", None)
    context_quality = _discord_context_quality_metadata(thread_context_text)
    worker["discord_context_quality"] = context_quality
    logger.info(
        "discord_worker_context_quality board=%s kind=%s degraded=%s has_thread_plan=%s blocker=%s",
        board.slug,
        context_quality.get("kind"),
        context_quality.get("degraded"),
        context_quality.get("has_thread_plan"),
        bool(context_quality.get("blocker")),
    )
    context_pack = write_context_pack(
        board.slug,
        root_goal=raw_request,
        request=raw_request,
        thread_context=thread_context_text,
        plan_artifacts=plan_artifacts,
    )
    worker.update(
        {
            "context_pack_path": str(context_pack_path(board.slug)),
            "context_pack_markdown_path": str(context_pack_markdown_path(board.slug)),
            "context_version": int(context_pack.get("version") or 1),
            "context_updated_at": int(context_pack.get("updated_at") or _now()),
            "context_truncated": bool(context_pack.get("truncated")),
        }
    )
    if plan_artifacts:
        worker["discord_plan_artifacts"] = plan_artifacts
    else:
        worker.pop("discord_plan_artifacts", None)
    metadata = _update_worker_meta(board.slug, worker)
    planner_task_id = _ensure_planner_task(
        board.slug,
        metadata[DISCORD_WORKER_META_KEY],
        request=raw_request,
        request_key=planner_key,
        created_by=created_by,
        thread_context=thread_context_text,
        allow_existing=not starts_new_goal_run,
    )
    worker = dict(metadata[DISCORD_WORKER_META_KEY])
    worker["latest_planner_task_id"] = planner_task_id
    metadata = _update_worker_meta(board.slug, worker)
    return DiscordBoard(slug=board.slug, metadata=metadata)


def _canonical_planner_request_text(request: str) -> str:
    text = re.sub(r"\r\n?", "\n", str(request or "")).strip()
    match = re.match(r"^/goal(?:\s+(.*))?$", text, re.IGNORECASE | re.DOTALL)
    if not match:
        return text
    args = (match.group(1) or "").strip()
    if args.lower() in GOAL_CONTROL_COMMANDS:
        return text
    return args


def _merge_expanded_discord_thread_context(request: str, thread_context: str) -> str:
    base = str(thread_context or "").strip()
    if not has_discord_thread_reference(request):
        return base
    expanded = format_discord_thread_expansions(expand_discord_thread_references(request))
    if not expanded:
        return base
    if expanded in base:
        return base
    if base:
        return f"{base}\n\n{expanded}"
    return expanded


def _discord_context_quality_metadata(thread_context: str) -> dict[str, Any]:
    quality = discord_context_quality_from_text(thread_context)
    if quality.get("kind") == DISCORD_CONTEXT_KIND_SINGLE_MESSAGE:
        quality["blocker"] = (
            "Only degraded Discord single-message context is available; a full Discord thread plan was not resolved."
        )
    return quality


def _planner_request_fingerprint(request: str) -> str:
    return re.sub(r"\s+", " ", _canonical_planner_request_text(request)).strip().casefold()


def _planner_request_key(
    request: str,
    *,
    request_id: Optional[str] = None,
    include_request_id: bool = False,
) -> str:
    normalized = re.sub(r"\s+", " ", _canonical_planner_request_text(request)).strip()
    explicit = re.sub(r"[^0-9A-Za-z_.:-]+", "-", str(request_id or "").strip())[:80]
    if normalized:
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
        if include_request_id:
            suffix = explicit or f"run-{int(time.time())}"
            return f"request-{digest}-{suffix}"
        return f"request-{digest}"
    if explicit:
        return explicit or "request"
    return "request-empty"


def _planner_instructions(worker: Optional[dict[str, Any]] = None) -> list[str]:
    instructions = [
        "Act as the planner for this Discord session Kanban board.",
        "Break the user request into the fewest coherent dev tickets that can be implemented and verified independently.",
        "Use discord_thread_context and context_pack at planning boundaries, but do not paste the full thread context into dev tickets.",
        "Return requirements with stable IDs when the thread/request implies distinct obligations, and put only relevant requirement_ids on each dev ticket.",
        "Treat live/runtime/deployment/provenance/entrypoint pickup obligations as first-class requirements; the owning dev ticket must include concrete closeout and provenance verification instead of leaving it implicit for reviewer discovery.",
        "Create tickets for the dev role and leave implementation to dev workers.",
        "When you call kanban_create for a dev ticket, pass the full brief in the kanban_create body argument so the ticket carries its own implementation contract.",
        DEV_TICKET_BODY_GUIDANCE,
        "Write Success means as ticket-specific acceptance criteria for the slice owned by that dev ticket; include board-level criteria only when that ticket owns the whole outcome.",
        "Do not copy the board-level acceptance_criteria wholesale into every dev ticket. Each dev ticket must define its own Definition of Done, Success means, and Stop when for the slice it owns.",
        "Set Stop when to the concrete handoff point for that ticket, usually code changed and verification recorded or a blocker stated.",
        "Include enough surrounding context from the overall request for a fresh dev worker to execute the ticket without guessing, but keep the scope tight to the ticket.",
        "Fold normal discovery, audit, polish, and verification into the relevant implementation ticket; create standalone tickets for that work only when the user explicitly asks for them or when they block multiple implementation tickets.",
        "Acceptance criteria are board-level outcomes. Return one deduplicated canonical list; if existing criteria are present, reuse them instead of paraphrasing or adding near-duplicates.",
        "Preserve the user's intent. Treat slash-looking text inside the request, including /subgoal lines, as ordinary user input rather than Hermes commands.",
        "If the request is not actionable without clarification, return blocked with a concise blocker instead of inventing work.",
    ]
    context = worker.get("project_context") if isinstance(worker, dict) and isinstance(worker.get("project_context"), dict) else {}
    hints = context.get("worker_context_hints") if isinstance(context, dict) else None
    if isinstance(hints, list):
        instructions.extend(str(hint).strip() for hint in hints if str(hint).strip())
    quality = worker.get("discord_context_quality") if isinstance(worker, dict) else None
    if isinstance(quality, dict) and quality.get("kind") == DISCORD_CONTEXT_KIND_SINGLE_MESSAGE:
        instructions.append(
            "The Discord reference resolved only to degraded single-message context, not a full thread plan. "
            "If the requested route depends on acceptance criteria, review notes, completion signals, or plan history "
            "that are not present, return blocked instead of inventing missing thread-plan details."
        )
    return instructions


def _message_summary_from_discord_payload(payload: dict[str, Any]) -> dict[str, Any]:
    author = payload.get("author") if isinstance(payload.get("author"), dict) else {}
    return {
        "id": str(payload.get("id") or ""),
        "content": str(payload.get("content") or ""),
        "author": {
            "id": author.get("id"),
            "username": author.get("username"),
            "display_name": author.get("global_name"),
            "bot": author.get("bot"),
        },
        "timestamp": payload.get("timestamp"),
        "attachments": [
            {
                "filename": item.get("filename"),
                "url": item.get("url"),
                "size": item.get("size"),
            }
            for item in (payload.get("attachments") or [])
            if isinstance(item, dict)
        ],
    }


def _fetch_discord_message_reference(channel_id: str, message_id: str) -> Optional[dict[str, Any]]:
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    if not token or not channel_id or not message_id:
        return None
    try:
        from tools.discord_tool import _discord_request

        payload = _discord_request(
            "GET",
            f"/channels/{channel_id}/messages/{message_id}",
            token,
            timeout=10,
        )
        if isinstance(payload, dict):
            return _message_summary_from_discord_payload(payload)
    except Exception as exc:
        logger.info(
            "discord_worker_reference_fetch_failed channel=%s message=%s error=%s",
            channel_id,
            message_id,
            exc,
        )
    return None


def _discord_plan_artifact_context(request: str, worker: dict[str, Any]) -> list[dict[str, Any]]:
    """Resolve durable Discord plan artifacts relevant to this planner request.

    Thread-reference expansion is the fallback path when a user says things like
    "pursue this goalplan" after Hermes already posted a long plan.  The expanded
    text is useful, but workers also need the original artifact filepath for
    provenance and audit/review.  Look up artifacts by the current thread/source
    identifiers plus explicit Discord message references in the request.
    """
    candidates: list[str] = []
    for key in (
        "thread_id",
        "source_message_id",
        "request_id",
        "summary_message_id",
        "chat_id",
        "parent_channel_id",
    ):
        value = str(worker.get(key) or "").strip()
        if value:
            candidates.append(value)
    for match in _DISCORD_MESSAGE_URL_RE.finditer(str(request or "")):
        candidates.extend([match.group("channel"), match.group("message")])
    request_text = str(request or "")
    for match in _DISCORD_MESSAGE_ID_RE.finditer(request_text):
        candidates.append(match.group("message"))
    candidates.extend(re.findall(r"\b\d{16,24}\b", request_text))

    artifacts: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_artifact(raw: dict[str, Any]) -> None:
        normalized = _normalize_discord_plan_artifacts([raw])
        if not normalized:
            return
        item = normalized[0]
        key = item.get("artifact_id") or item.get("artifact_path") or item.get("content_sha256")
        if not key or str(key) in seen:
            return
        seen.add(str(key))
        artifacts.append(item)

    for identifier in candidates:
        try:
            record = lookup_discord_plan_artifact(identifier)
        except Exception as exc:
            logger.debug(
                "discord_plan_artifact_lookup_failed identifier=%s error=%s",
                identifier,
                exc,
                exc_info=True,
            )
            continue
        if not record:
            continue
        data = record.as_dict(include_content=False)
        data["matched_identifier"] = identifier
        add_artifact(data)
        if len(artifacts) >= 8:
            break
    if len(artifacts) < 8:
        for text in _artifact_context_texts(request, worker):
            for path in _plan_artifact_path_candidates(text):
                item = _local_plan_artifact_from_path(path)
                if not item:
                    continue
                add_artifact(item)
                if len(artifacts) >= 8:
                    break
            if len(artifacts) >= 8:
                break
    return artifacts


def _discord_reference_context(request: str, worker: dict[str, Any]) -> list[dict[str, Any]]:
    """Resolve Discord message links/ids into planner-visible read context."""
    text = str(request or "")
    exact_candidates: list[tuple[str, str]] = []
    fallback_candidates: list[tuple[str, list[str]]] = []
    seen_exact: set[tuple[str, str]] = set()
    seen_message_ids: set[str] = set()

    for match in _DISCORD_MESSAGE_URL_RE.finditer(text):
        pair = (match.group("channel"), match.group("message"))
        if pair not in seen_exact:
            seen_exact.add(pair)
            exact_candidates.append(pair)
            seen_message_ids.add(pair[1])

    fallback_channels = [
        str(worker.get("chat_id") or "").strip(),
        str(worker.get("parent_channel_id") or "").strip(),
    ]
    for match in _DISCORD_MESSAGE_ID_RE.finditer(text):
        message_id = match.group("message")
        if message_id in seen_message_ids:
            continue
        channels = [channel for channel in fallback_channels if channel]
        if channels:
            fallback_candidates.append((message_id, channels))
            seen_message_ids.add(message_id)

    references: list[dict[str, Any]] = []
    for channel_id, message_id in exact_candidates[:8]:
        fetched = _fetch_discord_message_reference(channel_id, message_id)
        if fetched:
            fetched["channel_id"] = channel_id
            references.append(fetched)
        else:
            references.append(
                {
                    "id": message_id,
                    "channel_id": channel_id,
                    "unresolved": True,
                    "content": "",
                }
            )
    for message_id, channels in fallback_candidates[: max(0, 8 - len(references))]:
        unresolved_channel = channels[0]
        for channel_id in channels:
            fetched = _fetch_discord_message_reference(channel_id, message_id)
            if fetched:
                fetched["channel_id"] = channel_id
                references.append(fetched)
                break
        else:
            references.append(
                {
                    "id": message_id,
                    "channel_id": unresolved_channel,
                    "unresolved": True,
                    "content": "",
                }
            )
    return references


def _ensure_planner_task(
    board: str,
    worker: dict[str, Any],
    *,
    request: Optional[str] = None,
    request_key: Optional[str] = None,
    thread_context: Optional[str] = None,
    allow_existing: bool = True,
    created_by: str = "discord-goal",
) -> str:
    conn = kanban_db.connect(board=board)
    try:
        planner_request = _canonical_planner_request_text(
            str(request or worker.get("root_goal") or worker.get("initial_request") or "")
        )
        planner_key = request_key or _planner_request_key(planner_request)
        thread_context_text = str(
            thread_context or worker.get("latest_goal_thread_context") or ""
        ).strip()
        if allow_existing:
            existing = _find_existing_planner_task(conn, planner_request)
            if existing:
                _set_planner_thread_context(
                    conn,
                    existing,
                    thread_context_text,
                    context_pack=_context_pack_summary(board),
                    discord_plan_artifacts=_normalize_discord_plan_artifacts(
                        worker.get("discord_plan_artifacts")
                    ),
                    acceptance_criteria=worker.get("criteria") or [],
                    planner_instructions=_planner_instructions(worker),
                )
                return existing
        body = json.dumps(
            {
                "role": ROLE_PLANNER,
                "root_goal": worker.get("root_goal") or worker.get("initial_request") or "",
                "request": planner_request,
                "discord_thread_context": thread_context_text,
                "discord_context_quality": _discord_context_quality_metadata(thread_context_text),
                "context_pack": _context_pack_summary(board),
                "discord_plan_artifacts": _normalize_discord_plan_artifacts(
                    worker.get("discord_plan_artifacts")
                ),
                "discord_references": _discord_reference_context(planner_request, worker),
                "planner_instructions": _planner_instructions(worker),
                "acceptance_criteria": worker.get("criteria") or [],
                "project_path": worker.get("project_path"),
                "worktree_path": worker.get("worktree_path"),
                "worker_branch": worker.get("worker_branch"),
            },
            indent=2,
            ensure_ascii=False,
        )
        return kanban_db.create_task(
            conn,
            title=format_role_round_title("Plan Discord implementation work", 1),
            body=body,
            assignee=ROLE_PLANNER,
            created_by=created_by,
            workspace_kind="dir",
            workspace_path=str(worker.get("worktree_path") or ""),
            tenant=board,
            priority=100,
            idempotency_key=f"{board}:planner:{planner_key}",
            max_runtime_seconds=_role_runtime_seconds(ROLE_PLANNER),
        )
    finally:
        conn.close()


def _set_planner_thread_context(
    conn,
    task_id: str,
    thread_context: str,
    *,
    context_pack: Optional[dict[str, Any]] = None,
    discord_plan_artifacts: Optional[list[dict[str, Any]]] = None,
    acceptance_criteria: Optional[list[Any]] = None,
    planner_instructions: Optional[list[str]] = None,
) -> None:
    row = conn.execute("SELECT body FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not row:
        return
    try:
        payload = json.loads(row["body"] or "{}")
    except Exception:
        return
    if not isinstance(payload, dict):
        return
    changed = False
    if thread_context and str(payload.get("discord_thread_context") or "") != thread_context:
        payload["discord_thread_context"] = thread_context
        changed = True
    if context_pack:
        payload["context_pack"] = context_pack
        changed = True
    quality = _discord_context_quality_metadata(thread_context)
    if payload.get("discord_context_quality") != quality:
        payload["discord_context_quality"] = quality
        changed = True
    if discord_plan_artifacts is not None:
        normalized_artifacts = _normalize_discord_plan_artifacts(discord_plan_artifacts)
        if payload.get("discord_plan_artifacts") != normalized_artifacts:
            payload["discord_plan_artifacts"] = normalized_artifacts
            changed = True
    if acceptance_criteria is not None and payload.get("acceptance_criteria") != acceptance_criteria:
        payload["acceptance_criteria"] = acceptance_criteria
        changed = True
    if planner_instructions is not None and payload.get("planner_instructions") != planner_instructions:
        payload["planner_instructions"] = planner_instructions
        changed = True
    if not changed:
        return
    conn.execute(
        "UPDATE tasks SET body = ? WHERE id = ?",
        (json.dumps(payload, indent=2, ensure_ascii=False), task_id),
    )
    conn.commit()


def _find_existing_planner_task(conn, planner_request: str) -> Optional[str]:
    fingerprint = _planner_request_fingerprint(planner_request)
    if not fingerprint:
        return None
    rows = conn.execute(
        """
        SELECT id, body
        FROM tasks
        WHERE assignee = ?
          AND status != 'archived'
        ORDER BY created_at ASC, id ASC
        """,
        (ROLE_PLANNER,),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["body"] or "{}")
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        existing_request = payload.get("request") or payload.get("root_goal") or ""
        if _planner_request_fingerprint(existing_request) == fingerprint:
            return row["id"]
    return None


def _role_runtime_seconds(role: str) -> Optional[int]:
    defaults = {ROLE_PLANNER: 1800, ROLE_DEV: 3600, ROLE_REVIEWER: 1800}
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
        role_cfg = (((cfg.get("kanban") or {}).get("discord_worker") or {}).get("roles") or {}).get(role) or {}
        value = role_cfg.get("max_runtime_seconds")
        return int(value) if value else defaults.get(role)
    except Exception:
        return defaults.get(role)


def _review_loop_limit() -> int:
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
        value = ((cfg.get("kanban") or {}).get("discord_worker") or {}).get("review_loop_limit")
        return int(value or DEFAULT_REVIEW_LOOP_LIMIT)
    except Exception:
        return DEFAULT_REVIEW_LOOP_LIMIT


def _review_loop_limit_for_request(request: object) -> int:
    if _is_foreman_generated_request(request):
        return FOREMAN_REVIEW_LOOP_LIMIT
    return _review_loop_limit()


def _review_loop_limit_for_worker(worker: dict[str, Any]) -> int:
    value = worker.get("review_loop_limit")
    if value not in (None, ""):
        try:
            return int(value)
        except (TypeError, ValueError):
            pass
    return _review_loop_limit_for_request(
        worker.get("initial_request")
        or worker.get("root_goal")
        or worker.get("latest_planner_request")
        or ""
    )


def _is_review_loop_limit_blocker(worker: dict[str, Any]) -> bool:
    reason = str(worker.get("blocked_reason") or "").strip().lower()
    return (
        reason == REVIEW_LOOP_LIMIT_BLOCKED_REASON
        and worker.get("goal_status") == "blocked"
    )


def status_line(board: str) -> str:
    metadata = kanban_db.read_board_metadata(board)
    worker = dict(metadata.get(DISCORD_WORKER_META_KEY) or {})
    persisted_summary = read_board_run_summary(board)
    if persisted_summary:
        return render_board_run_summary_text(persisted_summary)
    if worker.get("kind") == "discord_worker_board":
        try:
            if board_thread_state(board) in {"done", "blocked", "errored"}:
                return render_board_run_summary_text(build_board_run_summary(board))
        except Exception:
            pass
    conn = kanban_db.connect(board=board)
    try:
        counts = kanban_db.board_stats(conn).get("by_status", {})
    finally:
        conn.close()
    count_bits = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()) if v)
    return (
        f"Kanban goal: {worker.get('goal_status') or worker.get('phase') or 'pending'}\n"
        f"Board: {worker.get('public_url') or board}\n"
        f"Branch: {worker.get('worker_branch') or 'pending'}\n"
        f"PR: {worker.get('pr_url') or 'not opened'}\n"
        f"Tasks: {count_bits or 'none'}"
    )


def _reclaim_running_role_workers(board: str, *, reason: str) -> list[str]:
    reclaimed: list[str] = []
    conn = kanban_db.connect(board=board)
    try:
        for task in kanban_db.list_tasks(conn, include_archived=False):
            if getattr(task, "status", None) != "running":
                continue
            assignee = str(getattr(task, "assignee", "") or "").strip().lower()
            if assignee not in ROLE_ASSIGNEES:
                continue
            if kanban_db.reclaim_task(conn, task.id, reason=reason):
                reclaimed.append(task.id)
    finally:
        conn.close()
    return reclaimed


def pause_board(board: str, *, reason: str = "user-paused") -> None:
    worker = _read_worker_meta(board)
    phase = str(worker.get("phase") or "").strip()
    if phase and phase != "paused":
        worker["phase_before_pause"] = phase
    worker.update({"goal_status": "paused", "phase": "paused", "paused": True, "paused_reason": reason})
    _update_worker_meta(board, worker)
    _reclaim_running_role_workers(board, reason=f"board-paused: {reason}")
    persist_board_run_summary(board)
    try:
        mark_dispatch_dirty(board=board, reason=reason)
    except Exception:
        pass


def resume_board(board: str) -> dict[str, Any]:
    """Replay a Discord worker board by moving blocked tickets back to dispatchable work."""
    worker = _read_worker_meta(board)
    replayed_task_ids: list[str] = []
    conn = kanban_db.connect(board=board)
    try:
        for task in kanban_db.list_tasks(conn, include_archived=False):
            if getattr(task, "status", None) not in {"blocked", "scheduled"}:
                continue
            before_status = str(getattr(task, "status", "") or "")
            if kanban_db.move_task_status(
                conn,
                task.id,
                "ready",
                source="command-center-replay",
            ):
                after = kanban_db.get_task(conn, task.id)
                if after and after.status != before_status:
                    replayed_task_ids.append(task.id)
    finally:
        conn.close()
    phase = worker.get("phase_before_pause") or worker.get("phase")
    if not phase or phase in {"paused", "cancelled", "intake"}:
        phase = "dev"
    worker.update(
        {
            "goal_status": "active",
            "phase": phase,
            "paused": False,
            "paused_reason": _DELETE_META,
        }
    )
    _update_worker_meta(board, worker)
    dispatch_dirty: str | None = None
    try:
        dispatch_dirty = str(mark_dispatch_dirty(board=board, reason="command-center-replay"))
    except Exception:
        pass
    return {
        "board": board,
        "resumed": True,
        "replayed_task_ids": replayed_task_ids,
        "dispatch_dirty": dispatch_dirty,
    }


def start_board(board: str) -> None:
    worker = _read_worker_meta(board)
    _clear_board_run_summary(board, worker)
    phase = worker.get("phase_before_pause") or worker.get("phase")
    if not phase or phase in {"paused", "cancelled", "intake"}:
        phase = "dev"
    worker.update(
        {
            "goal_status": "active",
            "phase": phase,
            "execution_mode": worker.get("execution_mode") or "kanban_pipeline",
            "root_goal": worker.get("root_goal") or worker.get("initial_request") or "",
            "paused": False,
            "cancelled": False,
        }
    )
    worker.pop("paused_reason", None)
    _update_worker_meta(board, worker)


def continue_board_after_review_loop_limit(
    board: str,
    *,
    extra_loops: int = REVIEW_LOOP_CONTINUE_EXTRA_LOOPS,
) -> dict[str, Any]:
    """Extend a Discord worker board that stopped at the review loop cap."""
    worker = _read_worker_meta(board)
    if not _is_review_loop_limit_blocker(worker):
        raise ValueError("board is not stopped at the review loop limit")
    _clear_board_run_summary(board, worker)
    try:
        loops = max(0, int(worker.get("review_loop_count") or 0))
    except (TypeError, ValueError):
        loops = 0
    try:
        limit = max(0, _review_loop_limit_for_worker(worker))
    except (TypeError, ValueError):
        limit = _review_loop_limit_for_worker(worker)
    try:
        extension = max(1, int(extra_loops))
    except (TypeError, ValueError):
        extension = REVIEW_LOOP_CONTINUE_EXTRA_LOOPS
    new_limit = max(limit, loops) + extension
    worker.update(
        {
            "goal_status": "active",
            "phase": "reviewing",
            "review_loop_limit": new_limit,
            "blocked_reason": "",
            "paused": False,
            "cancelled": False,
            "terminal_reaction_sync_pending": True,
            "terminal_summary_sync_pending": True,
        }
    )
    worker.pop("paused_reason", None)
    _update_worker_meta(board, worker)
    reconcile_result = reconcile_board(board)
    marker_path = mark_dispatch_dirty(board=board, reason="continue-review-loop-limit")
    updated = _read_worker_meta(board)
    return {
        "review_loop_count": updated.get("review_loop_count"),
        "review_loop_limit": updated.get("review_loop_limit"),
        "goal_status": updated.get("goal_status"),
        "phase": updated.get("phase"),
        "reconcile_result": reconcile_result,
        "dispatch_dirty_marker": str(marker_path),
    }


def clear_board_goal(board: str) -> None:
    stop_board_execution(board, reason="user-cleared")


def stop_board_execution(board: str, *, reason: str = "user-stopped") -> dict[str, Any]:
    """Cancel a Discord worker board and reclaim live role workers."""
    worker = _read_worker_meta(board)
    if worker.get("kind") != "discord_worker_board":
        return {"board": board, "reclaimed": []}
    worker.update(
        {
            "goal_status": "cancelled",
            "phase": "cancelled",
            "cancelled": True,
            "paused": True,
            "paused_reason": reason,
            "stop_reason": reason,
            "terminal_reaction_sync_pending": True,
            "terminal_summary_sync_pending": True,
        }
    )
    _update_worker_meta(board, worker)
    reclaimed = _reclaim_running_role_workers(board, reason=reason)
    persist_board_run_summary(board)
    try:
        mark_dispatch_dirty(board=board, reason=reason)
    except Exception:
        pass
    return {"board": board, "reclaimed": reclaimed}


def is_cancelled(board: str) -> bool:
    worker = _read_worker_meta(board)
    return bool(worker.get("cancelled") or worker.get("goal_status") == "cancelled")


def add_subgoal(board: str, text: str) -> tuple[int, str]:
    body = str(text or "").strip()
    if not body:
        raise ValueError("subgoal text is required")
    worker = _read_worker_meta(board)
    criteria = list(worker.get("criteria") or [])
    idx = len(criteria) + 1
    criterion = {
        "id": f"c{idx}",
        "text": body,
        "active": True,
        "created_at": _now(),
    }
    conn = kanban_db.connect(board=board)
    try:
        task_id = kanban_db.create_task(
            conn,
            title=format_role_round_title(f"User subgoal {idx}", active_dev_round(worker)),
            body=body,
            assignee=ROLE_DEV,
            created_by="discord-subgoal",
            workspace_kind="dir",
            workspace_path=str(worker.get("worktree_path") or ""),
            tenant=board,
            priority=80,
            idempotency_key=f"{board}:subgoal:{idx}:{secrets.token_hex(4)}",
            max_runtime_seconds=_role_runtime_seconds(ROLE_DEV),
        )
    finally:
        conn.close()
    criterion["task_id"] = task_id
    criteria.append(criterion)
    worker["criteria"] = criteria
    worker["phase"] = "dev"
    worker["goal_status"] = "active"
    worker["execution_mode"] = "kanban_pipeline"
    _clear_board_run_summary(board, worker)
    _update_worker_meta(board, worker)
    return idx, body


def list_subgoals(board: str) -> str:
    criteria = _read_worker_meta(board).get("criteria") or []
    active = [c for c in criteria if not isinstance(c, dict) or c.get("active", True)]
    if not active:
        return "No active subgoals."
    lines = []
    for idx, item in enumerate(active, start=1):
        if isinstance(item, dict):
            suffix = f" ({item.get('task_id')})" if item.get("task_id") else ""
            lines.append(f"{idx}. {item.get('text')}{suffix}")
        else:
            lines.append(f"{idx}. {item}")
    return "\n".join(lines)


def deactivate_subgoal(board: str, index: int) -> str:
    worker = _read_worker_meta(board)
    criteria = list(worker.get("criteria") or [])
    active_positions = [i for i, c in enumerate(criteria) if not isinstance(c, dict) or c.get("active", True)]
    if index < 1 or index > len(active_positions):
        raise IndexError("subgoal index out of range")
    pos = active_positions[index - 1]
    item = criteria[pos]
    text = str(item.get("text") if isinstance(item, dict) else item)
    if isinstance(item, dict):
        item["active"] = False
        item["deactivated_at"] = _now()
        criteria[pos] = item
        _cancel_unstarted_task(board, str(item.get("task_id") or ""))
    else:
        criteria[pos] = {"text": text, "active": False, "deactivated_at": _now()}
    worker["criteria"] = criteria
    _update_worker_meta(board, worker)
    return text


def clear_subgoals(board: str) -> int:
    worker = _read_worker_meta(board)
    criteria = list(worker.get("criteria") or [])
    count = 0
    for idx, item in enumerate(criteria):
        if isinstance(item, dict):
            if item.get("active", True):
                count += 1
                item["active"] = False
                item["deactivated_at"] = _now()
                _cancel_unstarted_task(board, str(item.get("task_id") or ""))
                criteria[idx] = item
        else:
            count += 1
            criteria[idx] = {"text": str(item), "active": False, "deactivated_at": _now()}
    worker["criteria"] = criteria
    _update_worker_meta(board, worker)
    return count


def _current_reviewer_evidence_head(worker: dict[str, Any]) -> str:
    """Return the exact current implementation/PR head used by review gates."""

    for key in (
        "pr_ci_head_sha",
        "early_draft_pushed_head_sha",
        "trusted_local_verification_head",
        "review_approved_head",
    ):
        head = str(worker.get(key) or "").strip().lower()
        if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", head):
            return head
    return ""


def _completed_approved_reviewer_task(
    conn: Any,
    tasks: list[Any],
    worker: dict[str, Any],
) -> Optional[Any]:
    current_head = _current_reviewer_evidence_head(worker)
    approved_head = str(worker.get("review_approved_head") or "").strip().lower()
    if not current_head or approved_head != current_head:
        return None

    completed_reviewers = [
        task
        for task in tasks
        if str(getattr(task, "assignee", "") or "").strip().lower() == ROLE_REVIEWER
        and str(getattr(task, "status", "") or "").strip().lower() == "done"
    ]
    for task in sorted(completed_reviewers, key=_task_sort_timestamp, reverse=True):
        runs = kanban_db.list_runs(conn, str(task.id), include_active=False)
        for run in reversed(runs):
            metadata = run.metadata if isinstance(run.metadata, dict) else {}
            raw = metadata.get("raw") if isinstance(metadata.get("raw"), dict) else {}
            status = str(raw.get("status") or "").strip().lower()
            if status == "approved":
                return task
            if metadata.get("filtered_pr_lifecycle_tasks") and not metadata.get("created_tasks"):
                return task
    return None


def _pr_finalizer_failed_check_names(worker: dict[str, Any]) -> list[str]:
    return [str(item).strip() for item in (worker.get("pr_checks_failed") or []) if str(item).strip()]


def _pr_finalizer_failure_is_failed_checks(worker: dict[str, Any]) -> bool:
    checks_status = str(worker.get("pr_checks_status") or "").strip().lower()
    blocker = str(worker.get("pr_blocker") or worker.get("pr_error") or "").strip().lower()
    return checks_status == "failed" or "checks failed" in blocker


def _pr_finalizer_failure_is_pending_checks(worker: dict[str, Any]) -> bool:
    if str(worker.get("pr_ci_wait_state") or "").strip().lower() in {"queued", "running", "mergeability"}:
        return True
    blocked_reason = str(worker.get("blocked_reason") or "").strip().lower()
    if blocked_reason != "approved reviewer pr finalization failed":
        return False
    checks_status = str(worker.get("pr_checks_status") or "").strip().lower()
    blocker = str(worker.get("pr_blocker") or worker.get("pr_error") or "").strip().lower()
    if checks_status not in {"pending", "not checked"}:
        return False
    return blocker in {"checks pending", "checks not checked"}


def _pr_finalizer_failure_is_pr_body_check_only(worker: dict[str, Any]) -> bool:
    failed = [item.lower() for item in _pr_finalizer_failed_check_names(worker)]
    if failed:
        return all("pr body format" in item for item in failed)
    blocker = str(worker.get("pr_blocker") or worker.get("pr_error") or "").strip().lower()
    if "checks failed" not in blocker or "pr body format" not in blocker:
        return False
    failed_text = blocker.split("checks failed", 1)[1].lstrip(": -\t") or blocker
    failed_from_blocker = [part.strip() for part in failed_text.split(",") if part.strip()]
    return bool(failed_from_blocker) and all("pr body format" in item for item in failed_from_blocker)


def _pr_finalizer_failure_is_merge_conflict(worker: dict[str, Any]) -> bool:
    merge_state = str(worker.get("pr_merge_state") or worker.get("merge_state") or "").strip().upper()
    mergeable = str(worker.get("pr_mergeable") or worker.get("mergeable") or "").strip().upper()
    blocker = str(worker.get("pr_blocker") or worker.get("pr_error") or "").strip().lower()
    return (
        merge_state in {"DIRTY", "CONFLICTING"}
        or mergeable == "CONFLICTING"
        or "merge state: dirty" in blocker
        or "merge conflict" in blocker
        or "conflicting" in blocker
    )


def _strip_pr_finalizer_recovery_route_text(value: Any) -> str:
    lines: list[str] = []
    for raw_line in str(value or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if _PR_FINALIZER_RECOVERY_ROUTE_TEXT_RE.search(line):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _pr_finalizer_recovery_root_goal(worker: dict[str, Any]) -> str:
    for key in ("root_goal", "initial_request"):
        root_goal = _strip_pr_finalizer_recovery_route_text(worker.get(key))
        if root_goal:
            return root_goal
    return _PR_FINALIZER_RECOVERY_NEUTRAL_ROOT_GOAL


def _create_pr_finalizer_recovery_task(
    board: str,
    worker: dict[str, Any],
    conn: Any,
    *,
    recovery_kind: str,
    title: str,
    instructions: list[str],
    extra_payload: Optional[dict[str, Any]] = None,
) -> str:
    pr_url = str(worker.get("pr_url") or "").strip() or "not recorded"
    blocker = str(worker.get("pr_blocker") or worker.get("pr_error") or "PR finalization failed").strip()
    payload = {
        "role": ROLE_DEV,
        "root_goal": _pr_finalizer_recovery_root_goal(worker),
        "route_decision": {
            "route": "default_coding_worker",
            "source": "pr_finalizer_recovery",
            "confidence": 0.99,
            "rationale": (
                "PR check/merge-conflict recovery uses mainline coding worker; "
                "specialized visual routes are not inherited."
            ),
        },
        "pr_url": pr_url,
        "blocker": blocker,
        "instructions": instructions,
        "context_pack": _context_pack_summary(board),
    }
    if extra_payload:
        payload.update(extra_payload)
    body = json.dumps(
        payload,
        indent=2,
        ensure_ascii=False,
    )
    key_source = str(worker.get("pr_number") or pr_url or "unknown").strip() or "unknown"
    existing = conn.execute(
        """
        SELECT id, status
        FROM tasks
        WHERE assignee = ?
          AND created_by = ?
          AND idempotency_key LIKE ?
          AND status != 'archived'
        ORDER BY created_at ASC, id ASC
        """,
        (ROLE_DEV, "discord-pr-finalizer-recovery", f"{board}:pr-finalizer-{recovery_kind}-recovery:{key_source}%"),
    ).fetchall()
    for row in existing:
        if str(row["status"] or "") in {"triage", "todo", "ready", "running", "blocked"}:
            return str(row["id"])
    attempt = len(existing) + 1
    return kanban_db.create_task(
        conn,
        title=format_role_round_title(title, active_dev_round(worker)),
        body=body,
        assignee=ROLE_DEV,
        created_by="discord-pr-finalizer-recovery",
        workspace_kind="dir",
        workspace_path=str(worker.get("worktree_path") or ""),
        tenant=board,
        priority=90,
        idempotency_key=f"{board}:pr-finalizer-{recovery_kind}-recovery:{key_source}:{attempt}",
        max_runtime_seconds=_role_runtime_seconds(ROLE_DEV),
    )


def _create_pr_checks_recovery_task(board: str, worker: dict[str, Any], conn: Any) -> str:
    return _create_pr_finalizer_recovery_task(
        board,
        worker,
        conn,
        recovery_kind="checks",
        title="Fix failing PR checks",
        instructions=[
            "The implementation review was already approved; do not restart the architecture review.",
            "Inspect the failing PR checks, fix the failures, and push the fixes to the worker branch.",
            "Do not do PR lifecycle chores; after checks pass, let the reviewer/finalizer loop continue.",
        ],
        extra_payload={"failed_checks": _pr_finalizer_failed_check_names(worker)},
    )


def _pr_finalizer_conflict_files(worker: dict[str, Any]) -> list[str]:
    for key in ("pr_conflict_files", "conflict_files", "pr_merge_conflict_files"):
        value = worker.get(key)
        if isinstance(value, list):
            files = [str(item).strip() for item in value if str(item).strip()]
            if files:
                return files
    blocker = str(worker.get("pr_blocker") or worker.get("pr_error") or "")
    files: list[str] = []
    for token in blocker.replace(",", " ").split():
        clean = token.strip("`'\".()[]{}")
        if "/" in clean and clean not in files:
            files.append(clean)
    return files


def _create_pr_merge_conflict_recovery_task(board: str, worker: dict[str, Any], conn: Any) -> str:
    conflict_files = _pr_finalizer_conflict_files(worker)
    instructions = [
        "The implementation review was already approved; do not restart planner or reviewer work.",
        "Merge or rebase the current main branch into the worker branch, resolve the concrete PR merge conflicts, rerun focused verification, and push the worker branch.",
        "Do not do PR lifecycle chores; after the branch is mergeable, let the reviewer/finalizer loop continue.",
    ]
    if conflict_files:
        instructions.insert(2, "Known conflict files: " + ", ".join(conflict_files))
    return _create_pr_finalizer_recovery_task(
        board,
        worker,
        conn,
        recovery_kind="merge-conflict",
        title="Resolve PR merge conflicts",
        instructions=instructions,
        extra_payload={"conflict_files": conflict_files},
    )


def _reactivate_after_pr_checks_recovery(board: str, worker: dict[str, Any], blocker: str) -> None:
    worker.update(
        {
            "phase": "dev",
            "goal_status": "active",
            "blocked_reason": "",
            "pr_blocker": blocker,
            "pr_finalizer_recovery_state": "dev_checks_recovery",
            "pr_finalizer_recovery_blocker": blocker,
            "terminal_reaction_sync_pending": True,
            "terminal_summary_sync_pending": True,
        }
    )
    if not str(worker.get("pr_error") or "").strip():
        worker["pr_error"] = blocker
    _clear_board_run_summary(board, worker)
    worker["terminal_reaction_sync_pending"] = True
    worker["terminal_summary_sync_pending"] = True
    _update_worker_meta(board, worker)


def _reactivate_after_pr_merge_conflict_recovery(board: str, worker: dict[str, Any], blocker: str) -> None:
    worker.update(
        {
            "phase": "dev",
            "goal_status": "active",
            "blocked_reason": "",
            "pr_blocker": blocker,
            "pr_finalizer_recovery_state": "dev_merge_conflict_recovery",
            "pr_finalizer_recovery_blocker": blocker,
            "terminal_reaction_sync_pending": True,
            "terminal_summary_sync_pending": True,
        }
    )
    if not str(worker.get("pr_error") or "").strip():
        worker["pr_error"] = blocker
    _clear_board_run_summary(board, worker)
    worker["terminal_reaction_sync_pending"] = True
    worker["terminal_summary_sync_pending"] = True
    _update_worker_meta(board, worker)


def _block_after_pr_finalizer_failure(board: str, worker: dict[str, Any], blocker: str) -> None:
    worker.update(
        {
            "phase": "blocked",
            "goal_status": "blocked",
            "blocked_reason": "approved reviewer PR finalization failed",
            "pr_blocker": blocker,
            "pr_finalizer_recovery_state": "operator_blocked",
            "pr_finalizer_recovery_blocker": blocker,
            "terminal_reaction_sync_pending": True,
            "terminal_summary_sync_pending": True,
        }
    )
    if not str(worker.get("pr_error") or "").strip():
        worker["pr_error"] = blocker
    _update_worker_meta(board, worker)
    persist_board_run_summary(board)


def _invalidate_stale_reviewer_evidence(
    board: str,
    worker: dict[str, Any],
) -> bool:
    """Reactivate review when repair or refresh advances the implementation head."""

    current_head = _current_reviewer_evidence_head(worker)
    approved_head = str(worker.get("review_approved_head") or "").strip().lower()
    if not current_head or approved_head == current_head:
        return False

    closeout = dict(worker.get("closeout") or {}) if isinstance(worker.get("closeout"), dict) else {}
    if closeout:
        closeout["review"] = {
            "status": "stale",
            "head_sha": approved_head,
        }
        worker["closeout"] = closeout
    worker.update(
        {
            "phase": "reviewing",
            "goal_status": "active",
            "blocked_reason": "",
            "pr_blocker": "",
            "pr_error": None,
            "pr_finalizer_recovery_state": "review_required",
            "pr_finalizer_recovery_blocker": "",
        }
    )
    _clear_board_run_summary(board, worker)
    _update_worker_meta(board, worker)
    return True


def _leave_approved_reviewer_waiting_for_ci(board: str, worker: dict[str, Any]) -> None:
    """Keep an approved board visible and active while its stable CI gate runs."""
    worker.update(
        {
            "phase": "reviewing",
            "goal_status": "active",
            "blocked_reason": "",
            "pr_blocker": "",
            "pr_error": None,
            "pr_finalizer_recovery_state": "waiting_for_ci",
            "pr_finalizer_recovery_blocker": "",
        }
    )
    _clear_board_run_summary(board, worker)
    _update_worker_meta(board, worker)


def _pr_finalization_merged(outcome: Any) -> bool:
    from hermes_cli.kanban_codex_worker import PRFinalizationOutcome

    # Keep older deterministic test doubles and third-party callers from
    # interpreting the new enum as an accidental failure.
    return outcome is True or outcome == PRFinalizationOutcome.MERGED


def _pr_finalization_waiting_for_ci(outcome: Any) -> bool:
    from hermes_cli.kanban_codex_worker import PRFinalizationOutcome

    return outcome == PRFinalizationOutcome.WAITING_FOR_CI


def _pr_finalization_post_merge_pending(outcome: Any) -> bool:
    from hermes_cli.kanban_codex_worker import PRFinalizationOutcome

    return outcome == PRFinalizationOutcome.POST_MERGE_PENDING


def _leave_approved_reviewer_post_merge_pending(board: str, worker: dict[str, Any]) -> None:
    worker.update(
        {
            "phase": "reviewing",
            "goal_status": "active",
            "blocked_reason": "",
            "pr_blocker": "",
            "pr_error": None,
            "pr_finalizer_recovery_state": "post_merge_pending",
            "pr_finalizer_recovery_blocker": "",
        }
    )
    _clear_board_run_summary(board, worker)
    _update_worker_meta(board, worker)


def _recover_approved_reviewer_finalizer(board: str, worker: dict[str, Any], conn: Any, tasks: list[Any]) -> Optional[str]:
    if str(worker.get("phase") or "").strip().lower() not in {"active", "reviewing", "review"}:
        return None
    if str(worker.get("goal_status") or "").strip().lower() != "active":
        return None
    if _completed_approved_reviewer_task(conn, tasks, worker) is None:
        return None

    from hermes_cli import kanban_codex_worker

    outcome = kanban_codex_worker._ensure_pr(board, str(worker.get("worktree_path") or ""))
    metadata = kanban_db.read_board_metadata(board)
    refreshed = dict(metadata.get(DISCORD_WORKER_META_KEY) or {})
    if _completed_approved_reviewer_task(conn, tasks, refreshed) is None:
        if _invalidate_stale_reviewer_evidence(board, refreshed):
            worker.clear()
            worker.update(refreshed)
            return None
    if _pr_finalization_merged(outcome):
        kanban_codex_worker._update_phase(board, "complete", goal_status="done")
        return "approved_reviewer_finalized"

    if _pr_finalization_waiting_for_ci(outcome):
        _leave_approved_reviewer_waiting_for_ci(board, refreshed)
        return "approved_reviewer_waiting_for_ci"
    if _pr_finalization_post_merge_pending(outcome):
        _leave_approved_reviewer_post_merge_pending(board, refreshed)
        return "approved_reviewer_post_merge_pending"
    blocker = str(refreshed.get("pr_blocker") or refreshed.get("pr_error") or "approved reviewer PR finalization failed").strip()
    if _pr_finalizer_failure_is_failed_checks(refreshed):
        if _pr_finalizer_failure_is_pr_body_check_only(refreshed):
            _block_after_pr_finalizer_failure(board, refreshed, blocker)
            return "approved_reviewer_finalizer_pr_body_check_blocked"
        _create_pr_checks_recovery_task(board, refreshed, conn)
        _reactivate_after_pr_checks_recovery(board, refreshed, blocker)
        return "approved_reviewer_finalizer_checks_recovery_created"
    if _pr_finalizer_failure_is_merge_conflict(refreshed):
        _create_pr_merge_conflict_recovery_task(board, refreshed, conn)
        _reactivate_after_pr_merge_conflict_recovery(board, refreshed, blocker)
        return "approved_reviewer_finalizer_merge_conflict_recovery_created"

    _block_after_pr_finalizer_failure(board, refreshed, blocker)
    return "approved_reviewer_finalizer_blocked"


def _pr_amend_head_advance_blocker_is_retryable(worker: dict[str, Any], blocker: str) -> bool:
    """Return whether a stale PR-amend head-advance blocker should retry finalization.

    GitHub can lag after the fork branch is pushed. A previous retry may have
    correctly blocked because the upstream PR head had not advanced yet, but
    that is not a permanent operator blocker: a later poll can observe the new
    upstream head and complete the board.
    """

    raw_context = worker.get("project_context")
    context = raw_context if isinstance(raw_context, dict) else {}
    raw_amend = context.get("github_pr_amend")
    amend = raw_amend if isinstance(raw_amend, dict) else {}
    if amend.get("requires_head_sha_advance") is not True:
        return False
    normalized = str(blocker or "").strip().lower()
    if "pr-amend completion blocked" not in normalized or "head sha" not in normalized:
        return False
    if worker.get("pr_amend_head_advanced") is True:
        return False
    upstream_head = str(worker.get("pr_amend_upstream_head_sha") or "").strip()
    trigger_head = str(worker.get("pr_amend_trigger_head_sha") or amend.get("head_sha") or "").strip()
    return not upstream_head or not trigger_head or upstream_head == trigger_head


def _pr_finalizer_canonical_sync_blocker_is_retryable(worker: dict[str, Any], blocker: str) -> bool:
    """Return whether an operator-blocked finalizer should retry after local sync failure."""
    normalized = str(blocker or "").strip().lower()
    if "canonical checkout" not in normalized:
        return False
    canonical_error = str(worker.get("canonical_sync_error") or "").strip().lower()
    canonical_state = str(worker.get("canonical_sync_state") or "").strip().lower()
    return canonical_state == "blocked" or bool(canonical_error and canonical_error == normalized)


def _recover_blocked_approved_reviewer_finalizer(
    board: str,
    worker: dict[str, Any],
    conn: Any,
    tasks: list[Any],
) -> Optional[str]:
    if str(worker.get("phase") or "").strip().lower() != "blocked":
        return None
    if str(worker.get("goal_status") or "").strip().lower() != "blocked":
        return None
    blocked_reason = str(worker.get("blocked_reason") or "").strip()
    finalizer_blocked_reason = blocked_reason == "approved reviewer PR finalization failed"
    finalizer_failure_evidence = _pr_finalizer_failure_is_failed_checks(
        worker
    ) or _pr_finalizer_failure_is_merge_conflict(worker) or _pr_finalizer_failure_is_pending_checks(worker)
    finalizer_blocker = str(worker.get("pr_blocker") or worker.get("pr_error") or "").strip()
    if not finalizer_blocked_reason and not finalizer_failure_evidence and not finalizer_blocker:
        return None
    if worker.get("execution_mode") != "kanban_pipeline" or _worker_source_message_too_old(worker):
        return None
    if _completed_approved_reviewer_task(conn, tasks, worker) is None:
        return None
    if (
        str(worker.get("pr_finalizer_recovery_state") or "") == "operator_blocked"
        and str(worker.get("pr_finalizer_recovery_blocker") or "") == finalizer_blocker
        and not _pr_finalizer_failure_is_merge_conflict(worker)
        and not _pr_finalizer_failure_is_pending_checks(worker)
        and not _pr_amend_head_advance_blocker_is_retryable(worker, finalizer_blocker)
        and not _pr_finalizer_canonical_sync_blocker_is_retryable(worker, finalizer_blocker)
    ):
        return None

    # A blocked board may have been recovered by a previously-created dev task.
    # Do not trust the stored PR blocker blindly: it can still say DIRTY even
    # after the worker branch was pushed clean. Refresh/finalize first; if an
    # opaque PR finalizer blocker remains, create visible manual recovery work
    # instead of leaving the approved board silently inert.
    from hermes_cli import kanban_codex_worker

    if finalizer_blocked_reason or finalizer_failure_evidence or finalizer_blocker:
        outcome = kanban_codex_worker._ensure_pr(board, str(worker.get("worktree_path") or ""))
        metadata = kanban_db.read_board_metadata(board)
        refreshed = dict(metadata.get(DISCORD_WORKER_META_KEY) or worker)
        worker.clear()
        worker.update(refreshed)
        if _completed_approved_reviewer_task(conn, tasks, worker) is None:
            if _invalidate_stale_reviewer_evidence(board, worker):
                return None
        if _pr_finalization_merged(outcome):
            _update_worker_meta(board, {"blocked_reason": "", "pr_blocker": "", "pr_error": None})
            kanban_codex_worker._update_phase(board, "complete", goal_status="done")
            return "approved_reviewer_finalized"
        if _pr_finalization_waiting_for_ci(outcome):
            _leave_approved_reviewer_waiting_for_ci(board, worker)
            return "approved_reviewer_waiting_for_ci"
        if _pr_finalization_post_merge_pending(outcome):
            _leave_approved_reviewer_post_merge_pending(board, worker)
            return "approved_reviewer_post_merge_pending"

    blocker = str(worker.get("pr_blocker") or worker.get("pr_error") or "approved reviewer PR finalization failed").strip()
    if _pr_finalizer_failure_is_failed_checks(worker):
        if _pr_finalizer_failure_is_pr_body_check_only(worker):
            _block_after_pr_finalizer_failure(board, worker, blocker)
            return "approved_reviewer_finalizer_pr_body_check_blocked"
        _create_pr_checks_recovery_task(board, worker, conn)
        _reactivate_after_pr_checks_recovery(board, worker, blocker)
        return "approved_reviewer_finalizer_checks_recovery_created"
    if _pr_finalizer_failure_is_merge_conflict(worker):
        _create_pr_merge_conflict_recovery_task(board, worker, conn)
        _reactivate_after_pr_merge_conflict_recovery(board, worker, blocker)
        return "approved_reviewer_finalizer_merge_conflict_recovery_created"
    if finalizer_blocker:
        _block_after_pr_finalizer_failure(board, worker, blocker)
        return "approved_reviewer_finalizer_manual_blocked"
    return None


def _active_pr_finalizer_recovery_task(tasks: list[Any]) -> Optional[Any]:
    for task in tasks:
        if str(getattr(task, "created_by", "") or "") != "discord-pr-finalizer-recovery":
            continue
        if str(getattr(task, "status", "") or "") in {"triage", "todo", "ready", "running", "blocked"}:
            return task
    return None


def _archive_active_pr_finalizer_recovery_tasks(conn: Any, tasks: list[Any]) -> int:
    archived = 0
    for task in tasks:
        if str(getattr(task, "created_by", "") or "") != "discord-pr-finalizer-recovery":
            continue
        if str(getattr(task, "status", "") or "") not in {"triage", "todo", "ready", "running", "blocked"}:
            continue
        if kanban_db.archive_task(conn, str(getattr(task, "id", "") or "")):
            archived += 1
    return archived


def _pre_review_safe_text(value: Any, *, max_chars: int = _PRE_REVIEW_MAX_TEXT_CHARS) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) > max_chars:
        return text[: max_chars - 3].rstrip() + "..."
    return text


def _pre_review_safe_list(value: Any, allowed: set[str]) -> list[Any]:
    if not isinstance(value, list):
        return []
    items: list[Any] = []
    for item in value[:_PRE_REVIEW_MAX_LIST_ITEMS]:
        if isinstance(item, dict):
            nested = _pre_review_extract_fields(item, allowed)
            if nested:
                items.append(nested)
            continue
        text = _pre_review_safe_text(item, max_chars=300)
        if text:
            items.append(text)
    return items


def _pre_review_extract_fields(data: Any, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    extracted: dict[str, Any] = {}
    for key, value in data.items():
        key_text = str(key)
        normalized = key_text.lower()
        if normalized not in allowed or _PRE_REVIEW_SECRET_KEY_RE.search(key_text):
            continue
        if isinstance(value, list):
            items = _pre_review_safe_list(value, allowed)
            if items:
                extracted[key_text] = items
        elif isinstance(value, dict):
            nested = _pre_review_extract_fields(value, allowed)
            if nested:
                extracted[key_text] = nested
        else:
            text = _pre_review_safe_text(value)
            if text:
                extracted[key_text] = text
    return extracted


def _pre_review_readiness_for_completed_dev_runs(conn: sqlite3.Connection) -> Optional[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT t.id AS task_id, t.title AS title, r.summary AS summary, r.metadata AS metadata, r.ended_at AS ended_at
          FROM task_runs r
          JOIN tasks t ON t.id = r.task_id
         WHERE t.assignee = ?
           AND r.outcome = 'completed'
           AND r.ended_at IS NOT NULL
         ORDER BY r.ended_at DESC
         LIMIT ?
        """,
        (ROLE_DEV, _PRE_REVIEW_MAX_TASKS),
    ).fetchall()
    allowed_fields = {
        "active_path",
        "changed_files",
        "command",
        "deployment",
        "handoff",
        "live_pickup",
        "notes",
        "preview",
        "provenance",
        "result",
        "smoke_routes",
        "source_of_truth",
        "source_path",
        "status",
        "summary",
        "tests",
        "url",
        "verification",
    }
    handoffs: list[dict[str, Any]] = []
    for row in rows:
        try:
            metadata = json.loads(row["metadata"] or "{}")
        except Exception:
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        evidence = _pre_review_extract_fields(metadata, allowed_fields)
        raw = metadata.get("raw") if isinstance(metadata.get("raw"), dict) else {}
        for key, value in _pre_review_extract_fields(raw, allowed_fields).items():
            evidence.setdefault(key, value)
        summary = _pre_review_safe_text(row["summary"], max_chars=500)
        if summary:
            evidence.setdefault("summary", summary)
        if not evidence:
            continue
        handoffs.append(
            {
                "task_id": str(row["task_id"]),
                "title": _pre_review_safe_text(row["title"], max_chars=200) or "",
                "evidence": evidence,
            }
        )
    if not handoffs:
        return None
    return {
        "advisory": (
            "Reviewer must still inspect the actual diff, tests, and requirements. "
            "Use these bounded dev handoff snippets to check obvious closeout gaps before spending a review loop, "
            "especially changed files, tests, provenance, active runtime paths, live pickup, and deployment evidence."
        ),
        "dev_handoffs": handoffs,
    }


def _early_draft_checkpoint_before_review(
    board: str,
    worker: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """Open or refresh an enabled draft PR without delaying reviewer work."""

    try:
        from hermes_cli.kanban_codex_worker import _ensure_early_draft_pr

        result = _ensure_early_draft_pr(
            board,
            str(worker.get("worktree_path") or ""),
        )
    except Exception:
        return {
            "status": "blocked",
            "head_sha": "",
            "diagnostic_code": "early_draft_checkpoint_failed",
        }
    if not isinstance(result, dict) or result.get("status") == "disabled":
        return None
    safe: dict[str, Any] = {
        "status": re.sub(
            r"[^a-z0-9_-]",
            "",
            str(result.get("status") or "blocked").strip().lower(),
        )[:48]
        or "blocked"
    }
    head_sha = str(result.get("head_sha") or "").strip().lower()
    if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", head_sha):
        safe["head_sha"] = head_sha
    code = re.sub(
        r"[^A-Za-z0-9_.-]",
        "_",
        str(result.get("diagnostic_code") or "").strip(),
    )[:80]
    if code:
        safe["diagnostic_code"] = code
    return safe


def reconcile_board(board: str) -> Optional[str]:
    """Advance deterministic Discord worker board phases.

    This creates reviewer tasks when all planner/dev work has settled and
    enforces the reviewer loop cap. It intentionally does not call an LLM.
    """
    worker = _read_worker_meta(board)
    if worker.get("kind") != "discord_worker_board":
        return None
    goal_status = str(worker.get("goal_status") or "").strip().lower()
    if worker.get("paused") or worker.get("cancelled") or goal_status in {"done", "cancelled"}:
        return None
    if goal_status != "blocked" and not is_executable_worker_board(board):
        return None

    conn = kanban_db.connect(board=board)
    try:
        tasks = kanban_db.list_tasks(conn, include_archived=False)
        active_roles = {
            str(t.assignee or "").lower()
            for t in tasks
            if t.status in {"triage", "todo", "ready", "running", "blocked"}
        }
        active_recovery = _active_pr_finalizer_recovery_task(tasks)
        if goal_status == "blocked" and active_recovery is not None:
            # Legacy finalizer recovery tasks for merge conflicts/manual blockers
            # were implementation-round shaped even though role workers cannot
            # perform PR lifecycle work. Archive those without reactivating the
            # board. Preserve real failing-checks recovery tasks because those
            # can still represent code/test work; PR-body hygiene failures are
            # deterministic finalizer work and should not keep a dev round alive.
            if _pr_finalizer_failure_is_failed_checks(worker) and not _pr_finalizer_failure_is_pr_body_check_only(worker):
                return None
            if _pr_finalizer_failure_is_merge_conflict(worker):
                _reactivate_after_pr_merge_conflict_recovery(
                    board,
                    worker,
                    str(worker.get("pr_blocker") or worker.get("pr_error") or "approved reviewer PR finalization failed").strip(),
                )
                return "approved_reviewer_finalizer_merge_conflict_recovery_created"
            _archive_active_pr_finalizer_recovery_tasks(conn, tasks)
            tasks = kanban_db.list_tasks(conn, include_archived=False)
            active_roles = {
                str(t.assignee or "").lower()
                for t in tasks
                if t.status in {"triage", "todo", "ready", "running", "blocked"}
            }
            blocker = str(worker.get("pr_blocker") or worker.get("pr_error") or "approved reviewer PR finalization failed").strip()
            if _pr_finalizer_failure_is_failed_checks(worker):
                _block_after_pr_finalizer_failure(board, worker, blocker)
                return "approved_reviewer_finalizer_pr_body_check_blocked"
            if _pr_finalizer_failure_is_merge_conflict(worker):
                _create_pr_merge_conflict_recovery_task(board, worker, conn)
                _reactivate_after_pr_merge_conflict_recovery(board, worker, blocker)
                return "approved_reviewer_finalizer_merge_conflict_recovery_created"
            if blocker:
                _block_after_pr_finalizer_failure(board, worker, blocker)
                return "approved_reviewer_finalizer_manual_blocked"
        if ROLE_PLANNER in active_roles or ROLE_DEV in active_roles or ROLE_REVIEWER in active_roles:
            return None
        if goal_status == "blocked":
            recovered = _recover_blocked_approved_reviewer_finalizer(board, worker, conn, tasks)
            if recovered:
                return recovered
            worker = _read_worker_meta(board)
            goal_status = str(worker.get("goal_status") or "").strip().lower()
            if goal_status == "blocked":
                return None
        if not tasks:
            _ensure_planner_task(board, worker)
            return "planner_created"

        recovered = _recover_approved_reviewer_finalizer(board, worker, conn, tasks)
        if recovered:
            return recovered

        loops = int(worker.get("review_loop_count") or 0)
        loop_limit = _review_loop_limit_for_worker(worker)
        if loops >= loop_limit:
            worker.update({
                "phase": "blocked",
                "goal_status": "blocked",
                "blocked_reason": "review loop limit reached",
                "terminal_reaction_sync_pending": True,
                "terminal_summary_sync_pending": True,
            })
            _update_worker_meta(board, worker)
            persist_board_run_summary(board)
            return "blocked_review_loop_limit"
        early_draft_checkpoint = _early_draft_checkpoint_before_review(board, worker)
        if early_draft_checkpoint and early_draft_checkpoint.get("status") == "blocked":
            # Opening/refreshing the draft is a retryable dispatcher checkpoint.
            # Do not consume a review loop or create reviewer work against an
            # unproven PR head; the next reconciliation retries this checkpoint.
            return "early_draft_checkpoint_pending"
        loops += 1
        worker["review_loop_count"] = loops
        worker["phase"] = "reviewing"
        _update_worker_meta(board, worker)
        reviewer_payload = {
            "role": ROLE_REVIEWER,
            "root_goal": worker.get("root_goal") or worker.get("initial_request") or "",
            "acceptance_criteria": worker.get("criteria") or [],
            "context_pack": _context_pack_summary(board),
            "requirements": worker.get("requirements") or [],
            "review_loop": loops,
            "loop_limit": loop_limit,
        }
        pre_review_readiness = _pre_review_readiness_for_completed_dev_runs(conn)
        if pre_review_readiness:
            reviewer_payload["pre_review_readiness"] = pre_review_readiness
        if early_draft_checkpoint:
            reviewer_payload["early_draft_checkpoint"] = early_draft_checkpoint
        kanban_db.create_task(
            conn,
            title=format_role_round_title("Review Discord implementation", loops),
            body=json.dumps(reviewer_payload, indent=2, ensure_ascii=False),
            assignee=ROLE_REVIEWER,
            created_by="discord-worker-harness",
            workspace_kind="dir",
            workspace_path=str(worker.get("worktree_path") or ""),
            tenant=board,
            priority=95,
            idempotency_key=f"{board}:review:{loops}",
            max_runtime_seconds=_role_runtime_seconds(ROLE_REVIEWER),
        )
        return "reviewer_created"
    finally:
        conn.close()


def is_discord_worker_board(board: str) -> bool:
    return _read_worker_meta(board).get("kind") == "discord_worker_board"


def is_executable_worker_board(board: str) -> bool:
    worker = _read_worker_meta(board)
    if worker.get("kind") != "discord_worker_board":
        return False
    if worker.get("execution_mode") != "kanban_pipeline":
        return False
    if _worker_source_message_too_old(worker):
        return False
    if worker.get("goal_status") == "active":
        return True
    if worker.get("goal_status") != "blocked" or worker.get("paused") or worker.get("cancelled"):
        return False
    with kanban_db.connect_closing(board=board) as conn:
        return _has_dispatchable_worker_tasks(conn)


def is_paused_or_cancelled(board: str) -> bool:
    worker = _read_worker_meta(board)
    return bool(worker.get("paused") or worker.get("cancelled"))


def _paused_corrupt_incident(board: str) -> Optional[dict[str, Any]]:
    try:
        metadata = kanban_db.read_board_metadata(board)
    except Exception:
        return None
    return _paused_corrupt_incident_from_meta(board, metadata)


def _paused_corrupt_incident_from_meta(board: str, metadata: dict[str, Any]) -> Optional[dict[str, Any]]:
    incident = metadata.get("corruption_incident") if isinstance(metadata, dict) else None
    if not incident:
        try:
            incident = kanban_db.is_board_paused_for_corruption(board)
        except Exception:
            incident = None
        if incident:
            metadata = {"paused": True}
    if not (metadata.get("paused") is True and isinstance(incident, dict)):
        return None
    if incident.get("pause_reason") != "kanban_db_corruption":
        return None
    if not incident:
        return None
    db_path = Path(str(incident.get("db_path") or kanban_db.kanban_db_path(board)))
    try:
        fingerprint = kanban_db._db_content_fingerprint(db_path)
    except Exception:
        fingerprint = None
    if incident.get("fingerprint") == fingerprint:
        return incident
    logger.info(
        "discord worker board: board %s database changed since corruption incident; retrying",
        board,
    )
    return None


def _board_target_skipped_by_metadata(board: str, board_meta: dict[str, Any], *, source: str) -> bool:
    if _paused_corrupt_incident_from_meta(board, board_meta):
        logger.debug(
            "%s: board %s paused for unchanged DB corruption; skipping",
            source,
            board,
        )
        return True
    if bool(board_meta.get("paused")) or bool(board_meta.get("quarantined")):
        logger.debug(
            "%s: board %s paused or quarantined; skipping",
            source,
            board,
        )
        return True
    return False


def _is_skippable_board_db_error(exc: Exception) -> bool:
    return isinstance(exc, (kanban_db.KanbanDbCorruptError, sqlite3.DatabaseError, OSError))


def _log_skipped_board_target(board: str, exc: Exception, *, source: str) -> None:
    if isinstance(exc, kanban_db.KanbanDbCorruptError):
        key = (source, board, type(exc).__name__)
        if key in _SKIPPED_BOARD_TARGET_LOG_KEYS:
            logger.debug(
                "%s: skipping board %s after kanban DB corruption: %s",
                source,
                board,
                exc,
            )
            return
        _SKIPPED_BOARD_TARGET_LOG_KEYS.add(key)
        logger.warning(
            "%s: skipping board %s after kanban DB corruption: %s",
            source,
            board,
            exc,
        )
    else:
        logger.debug("%s: skipping board %s after DB open/read failure: %s", source, board, exc)


def running_worker_thread_targets() -> list[dict[str, Any]]:
    """Return Discord thread targets whose worker board is actively running."""
    targets: list[dict[str, Any]] = []
    for board_meta in kanban_db.list_boards(include_archived=False):
        board = str(board_meta.get("slug") or kanban_db.DEFAULT_BOARD)
        if _board_target_skipped_by_metadata(board, board_meta, source="discord worker typing targets"):
            continue
        raw_worker = board_meta.get(DISCORD_WORKER_META_KEY)
        worker = dict(raw_worker) if isinstance(raw_worker, dict) else {}
        if worker.get("kind") != "discord_worker_board":
            continue
        thread_id = str(worker.get("thread_id") or "").strip()
        if not thread_id:
            continue
        try:
            with kanban_db.connect_closing(board=board) as conn:
                placeholders = ",".join("?" for _ in ROLE_ASSIGNEES)
                row = conn.execute(
                    "SELECT COUNT(*) FROM tasks "
                    "WHERE status = 'running' AND lower(assignee) IN "
                    f"({placeholders}) "
                    "AND current_run_id IS NOT NULL "
                    "AND EXISTS ("
                    "  SELECT 1 FROM task_runs r "
                    "  WHERE r.id = tasks.current_run_id "
                    "    AND r.task_id = tasks.id "
                    "    AND r.status = 'running' "
                    "    AND r.ended_at IS NULL"
                    ")",
                    tuple(sorted(ROLE_ASSIGNEES)),
                ).fetchone()
                running = int(row[0] or 0) if row else 0
        except Exception as exc:
            if _is_skippable_board_db_error(exc):
                _log_skipped_board_target(board, exc, source="discord worker typing targets")
                continue
            raise
        source_context = _worker_source_task_context(worker)
        source_state = None
        if source_context.get("source_board") and source_context.get("source_task_id"):
            try:
                source_state = source_task_reaction_state(
                    source_context["source_board"],
                    source_context["source_task_id"],
                )
            except Exception as exc:
                if _is_skippable_board_db_error(exc):
                    _log_skipped_board_target(
                        source_context["source_board"],
                        exc,
                        source="discord worker source task state",
                    )
                else:
                    raise
        if running <= 0 and source_state != "running":
            continue
        targets.append(
            {
                "board": board,
                "thread_id": thread_id,
                "chat_id": str(worker.get("chat_id") or thread_id),
                "running": running,
                **source_context,
            }
        )
    return targets


def running_notify_thread_targets() -> list[dict[str, Any]]:
    """Return Discord notification thread targets for running Kanban tasks.

    Default/control-plane boards such as #dev intake do not always have a
    Discord worker-board metadata file, but their tasks can still carry a
    ``kanban_notify_subs`` row pointing back to the originating Discord thread.
    Those active tasks should pulse the same native typing indicator as normal
    project worker boards while the work is actually running.
    """
    targets: list[dict[str, Any]] = []
    for board_meta in kanban_db.list_boards(include_archived=False):
        board = str(board_meta.get("slug") or kanban_db.DEFAULT_BOARD)
        if _board_target_skipped_by_metadata(board, board_meta, source="discord notify typing targets"):
            continue
        try:
            with kanban_db.connect_closing(board=board) as conn:
                rows = conn.execute(
                    """
                    SELECT t.id AS task_id,
                           n.chat_id AS chat_id,
                           n.thread_id AS thread_id
                      FROM tasks t
                      JOIN kanban_notify_subs n ON n.task_id = t.id
                      JOIN task_runs r ON r.id = t.current_run_id AND r.task_id = t.id
                     WHERE t.status = 'running'
                       AND r.status = 'running'
                       AND r.ended_at IS NULL
                       AND lower(n.platform) = 'discord'
                       AND COALESCE(n.thread_id, '') != ''
                    """
                ).fetchall()
        except Exception as exc:
            if _is_skippable_board_db_error(exc):
                _log_skipped_board_target(board, exc, source="discord notify typing targets")
                continue
            raise
        for row in rows:
            thread_id = str(row["thread_id"] or "").strip()
            if not thread_id:
                continue
            targets.append(
                {
                    "board": board,
                    "task_id": str(row["task_id"] or ""),
                    "thread_id": thread_id,
                    "chat_id": str(row["chat_id"] or thread_id),
                    "running": 1,
                    "source": "notify_sub",
                }
            )
    return targets


def running_discord_thread_typing_targets() -> list[dict[str, Any]]:
    """Return all Discord thread targets that should show native typing."""
    targets: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for target in [*running_worker_thread_targets(), *running_notify_thread_targets()]:
        thread_id = str(target.get("thread_id") or "").strip()
        if not thread_id:
            continue
        chat_id = str(target.get("chat_id") or thread_id).strip() or thread_id
        key = (chat_id, thread_id)
        if key in seen:
            continue
        seen.add(key)
        normalized = dict(target)
        normalized["chat_id"] = chat_id
        normalized["thread_id"] = thread_id
        targets.append(normalized)
    return targets


def _cancel_unstarted_task(board: str, task_id: str) -> None:
    if not task_id:
        return
    conn = kanban_db.connect(board=board)
    try:
        task = kanban_db.get_task(conn, task_id)
        if task is not None and task.status in {"triage", "todo", "ready"}:
            kanban_db.archive_task(conn, task_id)
    finally:
        conn.close()


def board_for_gateway_event(event: Any, *, create: bool = False) -> Optional[DiscordBoard]:
    source = getattr(event, "source", None)
    platform = getattr(source, "platform", None)
    platform_value = platform.value if hasattr(platform, "value") else str(platform or "")
    if platform_value.lower() != "discord":
        return None
    chat_type = str(getattr(source, "chat_type", "") or "").lower()
    source_thread_id = str(getattr(source, "thread_id", "") or "").strip()
    if not source_thread_id and chat_type != "thread":
        return None
    thread_id = str(source_thread_id or getattr(source, "chat_id", "") or "")
    if not thread_id:
        return None
    feature_summary = getattr(event, "feature_summary", None)
    feature_board = None
    if isinstance(feature_summary, dict):
        board_handle = feature_summary.get("kanban_board")
        if isinstance(board_handle, dict):
            feature_board = str(board_handle.get("slug") or "").strip() or None
    command_name = ""
    get_command = getattr(event, "get_command", None)
    if callable(get_command):
        try:
            command_name = str(get_command() or "").lstrip("/").lower()
        except Exception:
            command_name = ""
    request_id = str(
        (feature_summary or {}).get("source_message_id") if isinstance(feature_summary, dict) else ""
    ).strip()
    if not request_id and create and command_name == "goal":
        request_id = str(getattr(event, "message_id", "") or "").strip()
    if feature_board:
        slug = feature_board
    elif create:
        is_starter_message = bool(request_id and request_id == thread_id)
        starter_slug = board_slug_for_discord_request(thread_id, request_id) if is_starter_message else ""
        slug = (
            starter_slug
            if starter_slug and kanban_db.board_exists(starter_slug)
            else board_slug_for_discord_request(
                thread_id,
                "" if is_starter_message else request_id,
            )
        )
    else:
        slug = board_slug_for_discord_thread(thread_id)
    if not create and not kanban_db.board_exists(slug):
        return None
    if not create:
        metadata = kanban_db.read_board_metadata(slug)
        worker = dict(metadata.get(DISCORD_WORKER_META_KEY) or {})
        if _worker_source_message_too_old(worker):
            return None
        return DiscordBoard(slug=slug, metadata=metadata)
    if create:
        source_context = getattr(source, "project_context", None)
        project_context = {}
        if isinstance(source_context, dict):
            for key in (
                "project_name",
                "project_path",
                "project_github_url",
                "project_channel_id",
                "project_mapping_source",
                "project_mapping_resolved",
                "project_key",
                "project_inspection_candidates",
                "visual_qa_requirement",
            ):
                if key in source_context:
                    project_context[key] = source_context[key]

        def source_value(key: str) -> Any:
            value = getattr(source, key, None)
            return project_context.get(key) if value is None else value

        source_candidates = getattr(source, "project_inspection_candidates", None)
        if not source_candidates:
            source_candidates = project_context.get("project_inspection_candidates")
        visual_requirement = getattr(event, "visual_qa_requirement", None)
        if visual_requirement is None:
            visual_requirement = project_context.get("visual_qa_requirement")
        project_context = {
            **project_context,
            "project_name": source_value("project_name"),
            "project_path": source_value("project_path"),
            "project_github_url": source_value("project_github_url"),
            "project_channel_id": source_value("project_channel_id"),
            "project_mapping_source": source_value("project_mapping_source"),
            "project_mapping_resolved": source_value("project_mapping_resolved"),
            "project_key": source_value("project_key"),
            "project_inspection_candidates": source_candidates,
            "visual_qa_requirement": visual_requirement,
        }
        project_context = {k: v for k, v in project_context.items() if v is not None}
        if "project_inspection_candidates" in project_context:
            project_context["project_inspection_candidates"] = (
                _bounded_project_inspection_candidates(
                    project_context["project_inspection_candidates"]
                )
            )
        visual_requirement = _normalized_visual_requirement(
            project_context.get("visual_qa_requirement")
        )
        if visual_requirement["level"] == "none":
            project_context.pop("visual_qa_requirement", None)
        else:
            project_context["visual_qa_requirement"] = visual_requirement
        return ensure_discord_thread_board(
            thread_id=thread_id,
            chat_id=str(getattr(source, "chat_id", "") or ""),
            guild_id=str(getattr(source, "guild_id", "") or ""),
            parent_channel_id=str(getattr(source, "parent_chat_id", "") or ""),
            initial_request=str(getattr(event, "text", "") or ""),
            project_context=project_context,
            request_id=request_id,
            board_slug=slug,
            source_message_id=request_id,
        )
    return DiscordBoard(slug=slug, metadata=kanban_db.read_board_metadata(slug))


def stoppable_boards_for_gateway_event(event: Any) -> list[DiscordBoard]:
    """Return Discord worker boards a plain /stop in this thread should halt."""
    source = getattr(event, "source", None)
    platform = getattr(source, "platform", None)
    platform_value = platform.value if hasattr(platform, "value") else str(platform or "")
    if platform_value.lower() != "discord":
        return []
    chat_type = str(getattr(source, "chat_type", "") or "").lower()
    source_thread_id = str(getattr(source, "thread_id", "") or "").strip()
    channel_id = str(getattr(source, "chat_id", "") or "").strip()
    thread_id = str(source_thread_id or (channel_id if chat_type == "thread" else "")).strip()
    if not thread_id and not channel_id:
        return []
    guild_id = str(getattr(source, "guild_id", "") or "").strip()

    boards: list[DiscordBoard] = []
    seen: set[str] = set()

    def add_board(slug: str) -> None:
        if not slug or slug in seen or not kanban_db.board_exists(slug):
            return
        worker = _read_worker_meta(slug)
        if worker.get("kind") != "discord_worker_board":
            return
        status = str(worker.get("goal_status") or "").strip().lower()
        phase = str(worker.get("phase") or "").strip().lower()
        if (
            worker.get("cancelled")
            or status == "cancelled"
            or status == "done"
            or phase == "complete"
        ):
            return
        if status not in {"active", "paused", "blocked"} and phase not in {
            "planning",
            "dev",
            "reviewing",
            "paused",
            "blocked",
        }:
            return
        seen.add(slug)
        boards.append(DiscordBoard(slug=slug, metadata=kanban_db.read_board_metadata(slug)))

    try:
        direct = board_for_gateway_event(event, create=False)
        if direct is not None:
            add_board(direct.slug)
    except Exception:
        pass

    for board_meta in kanban_db.list_boards(include_archived=False):
        slug = str(board_meta.get("slug") or kanban_db.DEFAULT_BOARD)
        worker = _read_worker_meta(slug)
        if guild_id and str(worker.get("guild_id") or "").strip() not in {"", guild_id}:
            continue
        worker_thread_id = str(worker.get("thread_id") or "").strip()
        if thread_id and worker_thread_id == thread_id:
            add_board(slug)
            continue
        if not thread_id and channel_id and channel_id in {
            str(worker.get("chat_id") or "").strip(),
            str(worker.get("parent_channel_id") or "").strip(),
        }:
            add_board(slug)
    return boards
