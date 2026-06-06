"""Versioned proposal-run contract for cron self-improvement prongs.

The proposal layer is intentionally separate from Kanban execution state. Cron
jobs emit proposal runs and cards; a later approval path can turn a card into a
Kanban task with an idempotency key such as ``self-improvement:<proposal_id>``.
"""

from __future__ import annotations

import copy
import hashlib
import re
from datetime import datetime, timezone
from typing import Any

CONTRACT_VERSION = "self_improvement.proposal_run.v1"

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_STATUS_VALUES = {"proposed"}
_PRIORITY_VALUES = {"low", "medium", "high", "critical"}
_SEVERITY_VALUES = {"info", "minor", "major", "critical"}


class ProposalValidationError(ValueError):
    """Raised when a proposal-run payload does not match the v1 contract."""


def _require_dict(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProposalValidationError(f"{path} must be an object")
    return value


def _require_list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise ProposalValidationError(f"{path} must be an array")
    return value


def _require_text(value: Any, path: str, *, max_len: int | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProposalValidationError(f"{path} must be a non-empty string")
    text = value.strip()
    if max_len is not None and len(text) > max_len:
        raise ProposalValidationError(f"{path} must be at most {max_len} characters")
    return text


def _optional_text(value: Any, path: str, *, max_len: int | None = None) -> str | None:
    if value is None:
        return None
    return _require_text(value, path, max_len=max_len)


def _require_slug(value: Any, path: str) -> str:
    text = _require_text(value, path)
    if not _SLUG_RE.match(text):
        raise ProposalValidationError(f"{path} must be a lowercase slug")
    return text


def _require_datetime(value: Any, path: str) -> str:
    text = _require_text(value, path)
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ProposalValidationError(f"{path} must be an ISO-8601 datetime") from exc
    return text


def _as_config(config: dict[str, Any] | None) -> dict[str, Any]:
    if config is not None:
        return config
    try:
        from hermes_cli.config import load_config

        return load_config()
    except Exception:
        return {}


def _self_improvement_config(config: dict[str, Any] | None) -> dict[str, Any]:
    cfg = _as_config(config)
    section = cfg.get("self_improvement") if isinstance(cfg, dict) else None
    return section if isinstance(section, dict) else {}


def get_project_prong_config(
    project: str,
    prong: str,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return configured self-improvement project/prong metadata.

    Raises ``ProposalValidationError`` when the project or prong is not known.
    This keeps PID-first setup config-driven without hard-coding prongs in the
    validator itself.
    """

    project_slug = _require_slug(project, "project")
    prong_slug = _require_slug(prong, "prong")
    section = _self_improvement_config(config)
    projects = section.get("projects") if isinstance(section.get("projects"), dict) else {}
    project_cfg = projects.get(project_slug)
    if not isinstance(project_cfg, dict):
        raise ProposalValidationError(f"unknown self-improvement project: {project_slug}")
    prongs = project_cfg.get("prongs") if isinstance(project_cfg.get("prongs"), dict) else {}
    prong_cfg = prongs.get(prong_slug)
    if not isinstance(prong_cfg, dict):
        raise ProposalValidationError(
            f"unknown self-improvement prong for project {project_slug}: {prong_slug}"
        )
    merged = copy.deepcopy(prong_cfg)
    merged.setdefault("project", project_slug)
    merged.setdefault("prong", prong_slug)
    return merged


def derive_proposal_id(card: dict[str, Any], run: dict[str, Any], project: str, prong: str) -> str:
    """Build a stable proposal id from deterministic card/run inputs."""

    raw = "\n".join(
        [
            project,
            prong,
            str(run.get("cron_job_id") or ""),
            str(run.get("run_id") or ""),
            str(card.get("idempotency_key") or ""),
            str(card.get("title") or ""),
            str(card.get("summary") or ""),
        ]
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"{project}-{prong}-{digest}"


def _validate_source_excerpt(value: Any, path: str) -> dict[str, Any]:
    item = _require_dict(value, path)
    excerpt = {"text": _require_text(item.get("text"), f"{path}.text", max_len=2000)}
    for key in ("label", "url", "cron_output_path", "line_start", "line_end"):
        if key not in item or item[key] is None:
            continue
        if key in {"line_start", "line_end"}:
            if not isinstance(item[key], int) or item[key] < 1:
                raise ProposalValidationError(f"{path}.{key} must be a positive integer")
            excerpt[key] = item[key]
        else:
            excerpt[key] = _require_text(item[key], f"{path}.{key}", max_len=500)
    return excerpt


def _validate_card(value: Any, path: str, run: dict[str, Any], project: str, prong: str) -> dict[str, Any]:
    card = _require_dict(value, path)
    normalized: dict[str, Any] = {
        "title": _require_text(card.get("title"), f"{path}.title", max_len=140),
        "summary": _require_text(card.get("summary"), f"{path}.summary", max_len=500),
        "body": _require_text(card.get("body"), f"{path}.body", max_len=6000),
        "rationale": _require_text(card.get("rationale"), f"{path}.rationale", max_len=2000),
        "status": _require_text(card.get("status", "proposed"), f"{path}.status"),
        "created_at": _require_datetime(card.get("created_at"), f"{path}.created_at"),
    }
    if normalized["status"] not in _STATUS_VALUES:
        raise ProposalValidationError(f"{path}.status must be one of {sorted(_STATUS_VALUES)}")

    priority = _require_text(card.get("priority", "medium"), f"{path}.priority")
    if priority not in _PRIORITY_VALUES:
        raise ProposalValidationError(f"{path}.priority must be one of {sorted(_PRIORITY_VALUES)}")
    normalized["priority"] = priority

    severity = _optional_text(card.get("severity"), f"{path}.severity")
    if severity is not None:
        if severity not in _SEVERITY_VALUES:
            raise ProposalValidationError(f"{path}.severity must be one of {sorted(_SEVERITY_VALUES)}")
        normalized["severity"] = severity

    idempotency_key = _optional_text(card.get("idempotency_key"), f"{path}.idempotency_key", max_len=200)
    if idempotency_key is not None:
        normalized["idempotency_key"] = idempotency_key

    proposal_id = _optional_text(card.get("proposal_id"), f"{path}.proposal_id", max_len=120)
    normalized["proposal_id"] = proposal_id or derive_proposal_id(card, run, project, prong)

    source_excerpts = [_validate_source_excerpt(item, f"{path}.source_excerpts[{idx}]") for idx, item in enumerate(_require_list(card.get("source_excerpts", []), f"{path}.source_excerpts"))]
    normalized["source_excerpts"] = source_excerpts

    kanban = _require_dict(card.get("kanban_task"), f"{path}.kanban_task")
    normalized["kanban_task"] = {
        "title": _require_text(kanban.get("title"), f"{path}.kanban_task.title", max_len=140),
        "body": _require_text(kanban.get("body"), f"{path}.kanban_task.body", max_len=6000),
    }
    for key in ("assignee", "board", "tenant"):
        if kanban.get(key) is not None:
            normalized["kanban_task"][key] = _require_text(kanban[key], f"{path}.kanban_task.{key}", max_len=120)
    if kanban.get("tags") is not None:
        tags = [_require_slug(tag, f"{path}.kanban_task.tags[{idx}]") for idx, tag in enumerate(_require_list(kanban["tags"], f"{path}.kanban_task.tags"))]
        normalized["kanban_task"]["tags"] = tags
    return normalized


def validate_proposal_run(
    payload: dict[str, Any],
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate and normalize a v1 proposal-run payload."""

    root = _require_dict(payload, "payload")
    version = _require_text(root.get("contract_version"), "contract_version")
    if version != CONTRACT_VERSION:
        raise ProposalValidationError(f"contract_version must be {CONTRACT_VERSION}")

    project = _require_slug(root.get("project"), "project")
    prong = _require_slug(root.get("prong"), "prong")
    prong_cfg = get_project_prong_config(project, prong, config)
    max_cards = int(prong_cfg.get("max_cards_per_run") or _self_improvement_config(config).get("default_max_cards_per_run") or 5)
    if max_cards < 0:
        raise ProposalValidationError("max_cards_per_run must be non-negative")

    run = _require_dict(root.get("run"), "run")
    normalized_run = {
        "run_id": _require_text(run.get("run_id"), "run.run_id", max_len=120),
        "cron_job_id": _require_text(run.get("cron_job_id"), "run.cron_job_id", max_len=120),
        "created_at": _require_datetime(run.get("created_at"), "run.created_at"),
    }
    for key in ("cron_job_name", "cron_output_path", "source_url"):
        if run.get(key) is not None:
            normalized_run[key] = _require_text(run[key], f"run.{key}", max_len=500)
    if run.get("completed_at") is not None:
        normalized_run["completed_at"] = _require_datetime(run["completed_at"], "run.completed_at")

    cards = _require_list(root.get("cards"), "cards")
    if len(cards) > max_cards:
        raise ProposalValidationError(f"cards must contain at most {max_cards} items")

    normalized_cards = [
        _validate_card(card, f"cards[{idx}]", normalized_run, project, prong)
        for idx, card in enumerate(cards)
    ]
    proposal_ids = [card["proposal_id"] for card in normalized_cards]
    if len(set(proposal_ids)) != len(proposal_ids):
        raise ProposalValidationError("cards must have unique proposal_id values")

    generated_at = root.get("generated_at")
    normalized = {
        "contract_version": version,
        "project": project,
        "prong": prong,
        "run": normalized_run,
        "generated_at": _require_datetime(generated_at, "generated_at") if generated_at else datetime.now(timezone.utc).isoformat(),
        "human_markdown": _optional_text(root.get("human_markdown"), "human_markdown", max_len=12000) or "",
        "cards": normalized_cards,
    }
    return normalized


def build_cron_proposal_guidance(
    project: str,
    prong: str,
    config: dict[str, Any] | None = None,
) -> str:
    """Return prompt guidance for cron prongs that emit proposal runs."""

    prong_cfg = get_project_prong_config(project, prong, config)
    max_cards = int(prong_cfg.get("max_cards_per_run") or _self_improvement_config(config).get("default_max_cards_per_run") or 5)
    label = prong_cfg.get("label") or prong
    focus = prong_cfg.get("focus") or "discrete operator-improvement proposals"
    guidance = (
        "## Self-Improvement Proposal Output\n"
        f"Project: `{project}`. Prong: `{prong}` ({label}). Focus: {focus}\n\n"
        f"Emit at most {max_cards} proposal cards. Prefer zero cards over weak, duplicate, or wall-of-text output. "
        "Each card must be independently approvable as a future Kanban task. Do not create Kanban tasks, mutate dashboards, or approve/reject proposals.\n\n"
        "Return both: (1) a concise human markdown summary, and (2) one strict JSON block fenced as ```json containing a proposal run matching "
        f"`{CONTRACT_VERSION}`. The JSON root fields are: `contract_version`, `project`, `prong`, `run`, `generated_at`, `human_markdown`, and `cards`. "
        f"Set the JSON root exactly to `\"project\": \"{project}\"` and `\"prong\": \"{prong}\"`. "
        "The `run` object is only scheduler identity metadata and must include non-empty `run_id`, non-empty `cron_job_id`, and ISO-8601 `created_at`; "
        "it may also include `cron_job_name`, `cron_output_path`, `source_url`, and `completed_at`. Do not put audit notes, delegation details, status summaries, "
        "worker logs, or other card/review metadata into `run`. "
        "Each card needs `proposal_id` or a deterministic string `idempotency_key` (not an object), `title`, `summary`, `body`, `rationale`, "
        "`priority` as one of `critical`, `high`, `medium`, or `low` (do not use P0/P1/P2 labels), optional `severity` as one of "
        "`critical`, `major`, `minor`, or `info` (do not use high/medium/low severity labels), "
        "`source_excerpts` as objects with a `text` field, `status: proposed`, `created_at`, and `kanban_task` with enough title/body detail to construct a later Kanban task. "
        "Hard length limits: card `title` <= 140 chars, card `summary` <= 500 chars, card `body` <= 6000 chars, card `rationale` <= 2000 chars, "
        "each `source_excerpts[].text` <= 2000 chars, `kanban_task.title` <= 140 chars, and `kanban_task.body` <= 6000 chars. Keep summaries short; put detail in `body` and excerpts."
    )
    feedback_cfg = _self_improvement_config(config).get("feedback_context", {})
    if not isinstance(feedback_cfg, dict) or feedback_cfg.get("enabled", True):
        try:
            from self_improvement.proposal_storage import format_feedback_history_context, summarize_feedback_history

            summary = summarize_feedback_history(
                project=project,
                prong=prong,
                max_items_per_kind=int(feedback_cfg.get("max_items_per_kind", 3)) if isinstance(feedback_cfg, dict) else 3,
                max_text_chars=int(feedback_cfg.get("max_text_chars", 180)) if isinstance(feedback_cfg, dict) else 180,
            )
            guidance = f"{guidance}\n\n{format_feedback_history_context(summary)}"
        except Exception:
            pass
    return guidance
