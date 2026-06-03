"""Self-improvement proposal storage and approval helpers.

This module backs the Sligo Self-Improvement dashboard plugin.  It keeps the
upstream proposal lifecycle separate from Kanban execution state while giving
approved cards a narrow, idempotent path into Hermes' existing Kanban worker
machinery.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import quote

from hermes_cli.config import cfg_get, get_hermes_home, load_config
from hermes_cli import kanban_db

PARSER_VERSION = "self-improvement-proposals-v1"
MAX_CARDS_PER_RUN = 20
MAX_CARD_TEXT_CHARS = 8000
MAX_CARD_SUMMARY_CHARS = 1200
DEFAULT_STATUSES = {
    "proposed",
    "approved",
    "enqueued",
    "running",
    "done",
    "blocked",
    "failed",
    "rejected",
    "archived",
}
DEFAULT_VISIBLE_STATUSES = {"proposed", "approved", "enqueued", "running", "blocked", "failed", "done"}
_SECRET_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*[^\s`'\"]+"),
    re.compile(r"\b(?:sk|pk|ghp|gho|ghu|ghs|xox[baprs])-[-A-Za-z0-9_]{16,}\b"),
    re.compile(r"\b[A-Za-z0-9_=-]{32,}\.[A-Za-z0-9_=-]{16,}\.[A-Za-z0-9_=-]{16,}\b"),
)


@dataclass(frozen=True)
class ProngConfig:
    slug: str
    label: str
    cron_job_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProjectConfig:
    slug: str
    name: str
    kanban_board: str
    workspace_kind: str = "scratch"
    workspace_path: Optional[str] = None
    default_assignee: Optional[str] = None
    default_skills: tuple[str, ...] = ()
    prongs: tuple[ProngConfig, ...] = ()


DEFAULT_PROJECTS: tuple[ProjectConfig, ...] = (
    ProjectConfig(
        slug="pid",
        name="PID",
        kanban_board="pid",
        workspace_kind="scratch",
        workspace_path=None,
        default_assignee="codex",
        default_skills=("client-projects", "general-coding", "github-pr-workflow"),
        prongs=(
            ProngConfig("airflow-doctor", "Airflow Doctor", ("c89de076ba7c",)),
            ProngConfig("admin-dogfood", "Admin Dogfood UX", ("95478c11e1c8",)),
            ProngConfig("invisible-tech", "Invisible Technical Recommendations", ("1cfa836c7f7d",)),
            ProngConfig("visible-ux", "Visible UI/UX Recommendations", ("c31901b9e4c7",)),
        ),
    ),
)


class ProposalError(ValueError):
    """Domain-level validation error for proposal operations."""


def _now() -> int:
    return int(time.time())


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _json_loads(raw: Optional[str], default: Any) -> Any:
    if raw is None or raw == "":
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def _clean_slug(value: Any, *, field: str) -> str:
    text = str(value or "").strip().lower()
    if not text:
        raise ProposalError(f"{field} is required")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", text):
        raise ProposalError(f"{field} must be a slug of letters, numbers, dash, or underscore")
    return text


def _normalize_status(value: Optional[str]) -> str:
    status = str(value or "proposed").strip().lower()
    if status == "pending":
        status = "proposed"
    if status not in DEFAULT_STATUSES:
        raise ProposalError(f"status must be one of {sorted(DEFAULT_STATUSES)}")
    return status


def _redact(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    out = str(text)
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(lambda m: f"{m.group(1)}=[REDACTED]" if m.lastindex else "[REDACTED]", out)
    return out


def _truncate(text: Optional[str], limit: int = 8000) -> Optional[str]:
    if text is None:
        return None
    text = str(text)
    if len(text) <= limit:
        return text
    return text[: limit - 20] + "\n...[truncated]"


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _stable_id(prefix: str, *parts: Any) -> str:
    h = hashlib.sha1()
    for part in parts:
        h.update(str(part).encode("utf-8"))
        h.update(b"\0")
    return f"{prefix}_{h.hexdigest()[:16]}"


def _as_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value else ()
    if isinstance(value, Iterable):
        result: list[str] = []
        for item in value:
            text = str(item or "").strip()
            if text:
                result.append(text)
        return tuple(result)
    return ()


def _project_from_dict(slug: str, data: dict[str, Any]) -> ProjectConfig:
    prongs: list[ProngConfig] = []
    raw_prongs = data.get("prongs") if isinstance(data.get("prongs"), dict) else {}
    for prong_slug, p_data in raw_prongs.items():
        p_data = p_data if isinstance(p_data, dict) else {}
        prongs.append(
            ProngConfig(
                slug=_clean_slug(prong_slug, field="prong slug"),
                label=str(p_data.get("label") or prong_slug),
                cron_job_ids=_as_tuple(p_data.get("cron_job_ids")),
            )
        )
    return ProjectConfig(
        slug=_clean_slug(slug, field="project slug"),
        name=str(data.get("name") or slug),
        kanban_board=str(data.get("kanban_board") or slug),
        workspace_kind=str(data.get("workspace_kind") or "scratch"),
        workspace_path=str(data.get("workspace_path")) if data.get("workspace_path") else None,
        default_assignee=str(data.get("default_assignee")) if data.get("default_assignee") else None,
        default_skills=_as_tuple(data.get("default_skills")),
        prongs=tuple(prongs),
    )


def load_projects() -> list[ProjectConfig]:
    """Load project/prong config for the self-improvement surface.

    The UI never hardcodes PID.  If the operator has not configured this yet,
    we expose a safe default PID proving-ground shape with scratch workspaces so
    an approval cannot accidentally mutate a protected checkout.
    """
    try:
        config = load_config()
        raw = cfg_get(config, "self_improvement", "projects", default=None)
    except Exception:
        raw = None
    if isinstance(raw, dict) and raw:
        projects: list[ProjectConfig] = []
        for slug, data in raw.items():
            if isinstance(data, dict):
                projects.append(_project_from_dict(str(slug), data))
        if projects:
            return projects
    return list(DEFAULT_PROJECTS)


def project_map() -> dict[str, ProjectConfig]:
    return {p.slug: p for p in load_projects()}


def get_project(slug: str) -> ProjectConfig:
    slug = _clean_slug(slug, field="project")
    projects = project_map()
    if slug in projects:
        return projects[slug]
    # Project-agnostic v1: an unconfigured project is still reviewable and can
    # approve into a scratch Kanban board named after the project.  Persistent
    # workspace paths remain config-owned only.
    return ProjectConfig(slug=slug, name=slug, kanban_board=slug)


def get_prong(project: ProjectConfig, slug: str) -> ProngConfig:
    slug = _clean_slug(slug, field="prong")
    prongs = {p.slug: p for p in project.prongs}
    return prongs.get(slug) or ProngConfig(slug=slug, label=slug)


def db_path() -> Path:
    return get_hermes_home() / "self_improvement" / "proposals.db"


def connect(path: Optional[Path] = None) -> sqlite3.Connection:
    path = path or db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path: Optional[Path] = None) -> Path:
    path = path or db_path()
    conn = connect(path)
    try:
        conn.executescript(
            """
            PRAGMA journal_mode=WAL;

            CREATE TABLE IF NOT EXISTS proposal_runs (
                id TEXT PRIMARY KEY,
                project_slug TEXT NOT NULL,
                project_name TEXT NOT NULL,
                prong_slug TEXT NOT NULL,
                prong_name TEXT NOT NULL,
                cron_job_id TEXT,
                cron_job_name TEXT,
                cron_output_path TEXT,
                cron_output_sha256 TEXT,
                source_kind TEXT NOT NULL DEFAULT 'manual',
                source_created_at INTEGER,
                created_at INTEGER NOT NULL,
                model TEXT,
                provider TEXT,
                profile TEXT,
                workdir TEXT,
                raw_summary TEXT,
                parser_version TEXT NOT NULL,
                parse_status TEXT NOT NULL,
                parse_error TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_proposal_runs_project_prong
                ON proposal_runs(project_slug, prong_slug, created_at DESC);

            CREATE TABLE IF NOT EXISTS proposal_cards (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES proposal_runs(id) ON DELETE CASCADE,
                project_slug TEXT NOT NULL,
                prong_slug TEXT NOT NULL,
                title TEXT NOT NULL,
                summary TEXT,
                body TEXT,
                evidence_json TEXT NOT NULL DEFAULT '[]',
                recommended_action TEXT,
                worker_prompt TEXT,
                acceptance_criteria_json TEXT NOT NULL DEFAULT '[]',
                risk_notes TEXT,
                priority INTEGER NOT NULL DEFAULT 0,
                confidence REAL,
                estimated_effort TEXT,
                suggested_assignee TEXT,
                suggested_skills_json TEXT NOT NULL DEFAULT '[]',
                workspace_kind TEXT NOT NULL DEFAULT 'scratch',
                workspace_path TEXT,
                status TEXT NOT NULL DEFAULT 'proposed',
                kanban_board TEXT,
                kanban_task_id TEXT,
                worker_session_id TEXT,
                worker_public_url TEXT,
                idempotency_key TEXT UNIQUE,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                decided_at INTEGER,
                decided_by TEXT,
                decision_reason TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_proposal_cards_board
                ON proposal_cards(project_slug, prong_slug, status, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_proposal_cards_run
                ON proposal_cards(run_id, created_at ASC);

            CREATE TABLE IF NOT EXISTS proposal_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                proposal_id TEXT NOT NULL REFERENCES proposal_cards(id) ON DELETE CASCADE,
                run_id TEXT REFERENCES proposal_runs(id) ON DELETE SET NULL,
                project_slug TEXT NOT NULL,
                prong_slug TEXT NOT NULL,
                decision TEXT NOT NULL,
                rating INTEGER,
                strength TEXT,
                reason TEXT,
                operator TEXT,
                created_at INTEGER NOT NULL,
                kanban_task_id TEXT,
                outcome_status TEXT,
                outcome_summary TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_proposal_feedback_context
                ON proposal_feedback(project_slug, prong_slug, created_at DESC);
            """
        )
        conn.commit()
    finally:
        conn.close()
    return path


def _row_to_run(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def _row_to_card(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["evidence"] = _json_loads(data.pop("evidence_json", None), [])
    data["acceptance_criteria"] = _json_loads(data.pop("acceptance_criteria_json", None), [])
    data["suggested_skills"] = _json_loads(data.pop("suggested_skills_json", None), [])
    # Browser plugin aliases keep the public JSON compact and stable without
    # forcing the UI to know the storage column names.
    data["project"] = data.get("project_slug")
    data["prong"] = data.get("prong_slug")
    data["task_id"] = data.get("kanban_task_id")
    data["worker_url"] = data.get("worker_public_url")
    return data


def _row_to_feedback(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["kind"] = data.get("decision")
    data["feedback"] = data.get("reason")
    data["created_by"] = data.get("operator")
    return data


def list_project_payloads() -> list[dict[str, Any]]:
    return [
        {
            **asdict(project),
            "prongs": [asdict(prong) for prong in project.prongs],
        }
        for project in load_projects()
    ]


def list_projects() -> list[dict[str, Any]]:
    """Return configured/discovered projects with proposal counts."""
    init_db()
    configured = {
        project.slug: {
            "slug": project.slug,
            "project": project.slug,
            "project_slug": project.slug,
            "name": project.name,
            "kanban_board": project.kanban_board,
            "prongs": [asdict(prong) for prong in project.prongs],
            "proposal_count": 0,
            "pending_count": 0,
            "updated_at": None,
        }
        for project in load_projects()
    }
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT project_slug,
                   COUNT(*) AS proposal_count,
                   SUM(CASE WHEN status = 'proposed' THEN 1 ELSE 0 END) AS pending_count,
                   MAX(updated_at) AS updated_at
              FROM proposal_cards
             GROUP BY project_slug
            """
        ).fetchall()
    finally:
        conn.close()
    for row in rows:
        slug = row["project_slug"]
        project = configured.get(slug)
        if project is None:
            project = {
                "slug": slug,
                "project": slug,
                "project_slug": slug,
                "name": slug,
                "kanban_board": slug,
                "prongs": [],
                "proposal_count": 0,
                "pending_count": 0,
                "updated_at": None,
            }
            configured[slug] = project
        project["proposal_count"] = int(row["proposal_count"] or 0)
        project["pending_count"] = int(row["pending_count"] or 0)
        project["updated_at"] = row["updated_at"]
    return sorted(configured.values(), key=lambda item: str(item.get("project") or ""))


def _extract_json_candidates(text: str) -> list[Any]:
    """Parse explicit JSON proposal blocks from cron output.

    Do not use a non-greedy ``\\{.*?\\}`` regex here: proposal payloads contain
    nested card objects, so the first closing brace is almost never the end of
    the payload.  Marker and fence extraction should pass the whole block to
    ``json.loads`` and let the JSON parser validate structure.
    """
    candidates: list[str] = []

    marker_match = re.search(
        r"BEGIN_HERMES_SELF_IMPROVEMENT_PROPOSALS\s*(.*?)\s*END_HERMES_SELF_IMPROVEMENT_PROPOSALS",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    if marker_match:
        candidates.append(marker_match.group(1).strip())

    for match in re.finditer(
        r"```(?:json|self-improvement-proposals)?\s*\n?(.*?)\n?```",
        text,
        re.DOTALL | re.IGNORECASE,
    ):
        candidates.append(match.group(1).strip())

    candidates.append(text.strip())

    parsed: list[Any] = []
    for candidate in candidates:
        if not candidate:
            continue
        try:
            parsed.append(json.loads(candidate))
        except Exception:
            continue
    return parsed


def parse_proposal_payload(source_text: str, proposal_json: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Return a strict proposal payload with cards.

    The v1 cron-output contract is deliberately explicit: emit a JSON object
    with ``project``, ``prong`` and ``cards`` between these exact markers::

        BEGIN_HERMES_SELF_IMPROVEMENT_PROPOSALS
        {"project":"pid","prong":"visible-ux","cards":[...]}
        END_HERMES_SELF_IMPROVEMENT_PROPOSALS

    ``proposal_json`` may pass the same object directly for tests/operator
    imports.  Fenced JSON is also accepted for operator convenience, but
    arbitrary prose splitting remains intentionally unsupported.
    """
    if proposal_json is not None:
        payload = proposal_json
    else:
        payload = None
        for candidate in _extract_json_candidates(source_text or ""):
            if isinstance(candidate, dict) and isinstance(candidate.get("cards"), list):
                payload = candidate
                break
        if payload is None:
            raise ProposalError("no strict proposal JSON object with a cards array found")
    if not isinstance(payload, dict):
        raise ProposalError("proposal payload must be a JSON object")
    cards = payload.get("cards")
    if not isinstance(cards, list):
        raise ProposalError("proposal payload must include a cards array")
    if len(cards) > MAX_CARDS_PER_RUN:
        raise ProposalError(f"proposal payload has too many cards; max is {MAX_CARDS_PER_RUN}")
    return payload


def _bounded_text(raw: Any, *, field: str, limit: int, required: bool = False) -> str:
    text = str(raw or "").strip()
    if required and not text:
        raise ProposalError(f"{field} is required")
    if len(text) > limit:
        raise ProposalError(f"{field} exceeds {limit} characters")
    return _redact(text) or text


def _coerce_card(raw: dict[str, Any], *, run_id: str, project: ProjectConfig, prong: ProngConfig, index: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ProposalError(f"card {index + 1} must be an object")
    title = str(raw.get("title") or "").strip()
    if not title:
        raise ProposalError(f"card {index + 1} missing title")
    body_source = raw.get("body") or raw.get("rationale") or raw.get("recommended_action") or raw.get("worker_prompt") or raw.get("summary")
    body_text = _bounded_text(body_source, field=f"card {index + 1} body", limit=MAX_CARD_TEXT_CHARS, required=True)
    evidence = raw.get("evidence") or []
    if isinstance(evidence, str):
        evidence = [evidence]
    if not isinstance(evidence, list):
        raise ProposalError(f"card {index + 1} evidence must be a list or string")
    criteria = raw.get("acceptance_criteria") or raw.get("acceptance") or []
    if isinstance(criteria, str):
        criteria = [criteria]
    if not isinstance(criteria, list):
        raise ProposalError(f"card {index + 1} acceptance_criteria must be a list or string")
    skills = raw.get("suggested_skills") or raw.get("skills") or project.default_skills
    skills_tuple = _as_tuple(skills)
    priority = raw.get("priority", 0)
    try:
        priority_i = int(priority or 0)
    except Exception:
        priority_i = 0
    confidence = raw.get("confidence")
    try:
        confidence_f = float(confidence) if confidence is not None else None
    except Exception:
        confidence_f = None

    card_id = str(raw.get("id") or "").strip()
    if not card_id:
        card_id = _stable_id("prop", run_id, index, title, raw.get("summary") or "")
    else:
        card_id = _clean_slug(card_id.replace("prop_", ""), field="card id") if not card_id.startswith("prop_") else card_id

    # Workspace, board, assignee, and skills are resolved from trusted project
    # config with per-card suggestions only for assignee/skills.  Cards cannot
    # route execution to arbitrary filesystem paths.
    workspace_kind = project.workspace_kind or "scratch"
    if workspace_kind not in kanban_db.VALID_WORKSPACE_KINDS:
        raise ProposalError(f"project {project.slug} has invalid workspace_kind {workspace_kind!r}")

    return {
        "id": card_id,
        "run_id": run_id,
        "project_slug": project.slug,
        "prong_slug": prong.slug,
        "title": _bounded_text(title, field=f"card {index + 1} title", limit=240, required=True),
        "summary": _bounded_text(raw.get("summary") or "", field=f"card {index + 1} summary", limit=MAX_CARD_SUMMARY_CHARS),
        "body": body_text,
        "evidence_json": _json_dumps([_redact(str(item)) for item in evidence if item is not None]),
        "recommended_action": _bounded_text(raw.get("recommended_action") or "", field=f"card {index + 1} recommended_action", limit=2000),
        "worker_prompt": _bounded_text(raw.get("worker_prompt") or raw.get("prompt") or raw.get("recommended_action") or title, field=f"card {index + 1} worker_prompt", limit=MAX_CARD_TEXT_CHARS),
        "acceptance_criteria_json": _json_dumps([_redact(str(item)) for item in criteria if item is not None]),
        "risk_notes": _bounded_text(raw.get("risk_notes") or raw.get("risk") or "", field=f"card {index + 1} risk_notes", limit=2000),
        "priority": priority_i,
        "confidence": confidence_f,
        "estimated_effort": str(raw.get("estimated_effort") or raw.get("effort") or "").strip() or None,
        "suggested_assignee": str(raw.get("suggested_assignee") or project.default_assignee or "").strip() or None,
        "suggested_skills_json": _json_dumps(skills_tuple),
        "workspace_kind": workspace_kind,
        "workspace_path": project.workspace_path,
        "kanban_board": project.kanban_board,
        "idempotency_key": f"self-improvement:{card_id}",
    }


def ingest_proposals(
    *,
    run_id: Optional[str] = None,
    project_slug: Optional[str] = None,
    prong_slug: Optional[str] = None,
    cron_job_id: Optional[str] = None,
    cron_job_name: Optional[str] = None,
    cron_output_path: Optional[str] = None,
    cron_output_text: str = "",
    proposal_json: Optional[dict[str, Any]] = None,
    source_kind: str = "manual",
    source_created_at: Optional[int] = None,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    profile: Optional[str] = None,
    workdir: Optional[str] = None,
) -> dict[str, Any]:
    init_db()
    source_text = cron_output_text or ""
    output_hash = _hash_text(source_text) if source_text else None
    parse_status = "ok"
    parse_error = None
    try:
        payload = parse_proposal_payload(source_text, proposal_json)
        payload_project = payload.get("project") or project_slug
        payload_prong = payload.get("prong") or prong_slug
        project = get_project(str(payload_project or ""))
        prong = get_prong(project, str(payload_prong or ""))
        cards_src = payload["cards"]
    except Exception as exc:
        parse_status = "failed"
        parse_error = str(exc)
        if not project_slug or not prong_slug:
            raise
        project = get_project(project_slug)
        prong = get_prong(project, prong_slug)
        cards_src = []

    basis = output_hash or _json_dumps(proposal_json or {}) or str(_now())
    run_id = str(run_id or "").strip() or _stable_id("run", project.slug, prong.slug, cron_job_id or "manual", basis)
    now = _now()
    raw_summary = _truncate(_redact(source_text), 8000)

    cards: list[dict[str, Any]] = []
    conn = connect()
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO proposal_runs (
                    id, project_slug, project_name, prong_slug, prong_name,
                    cron_job_id, cron_job_name, cron_output_path, cron_output_sha256,
                    source_kind, source_created_at, created_at, model, provider,
                    profile, workdir, raw_summary, parser_version, parse_status, parse_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    cron_job_name = excluded.cron_job_name,
                    cron_output_path = excluded.cron_output_path,
                    raw_summary = excluded.raw_summary,
                    parse_status = excluded.parse_status,
                    parse_error = excluded.parse_error
                """,
                (
                    run_id,
                    project.slug,
                    project.name,
                    prong.slug,
                    prong.label,
                    cron_job_id,
                    cron_job_name,
                    cron_output_path,
                    output_hash,
                    source_kind or "manual",
                    source_created_at,
                    now,
                    model,
                    provider,
                    profile,
                    workdir,
                    raw_summary,
                    PARSER_VERSION,
                    parse_status,
                    parse_error,
                ),
            )
            if parse_status == "ok":
                for index, raw_card in enumerate(cards_src):
                    card = _coerce_card(raw_card, run_id=run_id, project=project, prong=prong, index=index)
                    cards.append(card)
                    conn.execute(
                        """
                        INSERT INTO proposal_cards (
                            id, run_id, project_slug, prong_slug, title, summary, body,
                            evidence_json, recommended_action, worker_prompt,
                            acceptance_criteria_json, risk_notes, priority, confidence,
                            estimated_effort, suggested_assignee, suggested_skills_json,
                            workspace_kind, workspace_path, status, kanban_board,
                            idempotency_key, created_at, updated_at
                        ) VALUES (
                            :id, :run_id, :project_slug, :prong_slug, :title, :summary, :body,
                            :evidence_json, :recommended_action, :worker_prompt,
                            :acceptance_criteria_json, :risk_notes, :priority, :confidence,
                            :estimated_effort, :suggested_assignee, :suggested_skills_json,
                            :workspace_kind, :workspace_path, 'proposed', :kanban_board,
                            :idempotency_key, :created_at, :updated_at
                        )
                        ON CONFLICT(id) DO UPDATE SET
                            title = excluded.title,
                            summary = excluded.summary,
                            body = excluded.body,
                            evidence_json = excluded.evidence_json,
                            recommended_action = excluded.recommended_action,
                            worker_prompt = excluded.worker_prompt,
                            acceptance_criteria_json = excluded.acceptance_criteria_json,
                            risk_notes = excluded.risk_notes,
                            priority = excluded.priority,
                            confidence = excluded.confidence,
                            estimated_effort = excluded.estimated_effort,
                            suggested_assignee = excluded.suggested_assignee,
                            suggested_skills_json = excluded.suggested_skills_json,
                            workspace_kind = excluded.workspace_kind,
                            workspace_path = excluded.workspace_path,
                            kanban_board = excluded.kanban_board,
                            updated_at = excluded.updated_at
                            WHERE proposal_cards.status = 'proposed'
                        """,
                        {**card, "created_at": now, "updated_at": now},
                    )
    finally:
        conn.close()

    return get_run(run_id, include_cards=True)


def ingest_run(payload: dict[str, Any]) -> dict[str, Any]:
    """Ingest a dashboard-friendly structured run payload.

    Accepts either the preferred ``cards`` key or the older ``proposals`` alias.
    When a payload carries cards from multiple prongs, v1 stores one proposal
    run per prong while returning a combined response for the browser/API.
    """
    if not isinstance(payload, dict):
        raise ProposalError("ingest payload must be an object")
    cards = payload.get("cards") if isinstance(payload.get("cards"), list) else payload.get("proposals")
    if not isinstance(cards, list):
        raise ProposalError("ingest payload must include a cards or proposals array")
    project = payload.get("project") or payload.get("project_id") or payload.get("project_slug")
    project_slug = _clean_slug(project, field="project")
    base_run_id = str(payload.get("run_id") or payload.get("id") or "").strip()
    source_text = str(payload.get("source_text") or payload.get("source") or payload.get("raw_summary") or "")
    groups: dict[str, list[dict[str, Any]]] = {}
    for raw in cards:
        if not isinstance(raw, dict):
            raise ProposalError("each proposal card must be an object")
        prong = raw.get("prong") or raw.get("prong_slug") or raw.get("area") or payload.get("prong") or "general"
        prong_slug = _clean_slug(prong, field="prong")
        groups.setdefault(prong_slug, []).append(raw)
    runs: list[dict[str, Any]] = []
    for prong_slug, prong_cards in groups.items():
        run_id = base_run_id
        if base_run_id and len(groups) > 1:
            run_id = f"{base_run_id}:{prong_slug}"
        run = ingest_proposals(
            run_id=run_id or None,
            project_slug=project_slug,
            prong_slug=prong_slug,
            cron_job_id=payload.get("cron_job_id"),
            cron_job_name=payload.get("cron_job_name"),
            cron_output_path=payload.get("cron_output_path"),
            cron_output_text=source_text,
            proposal_json={**payload, "project": project_slug, "prong": prong_slug, "cards": prong_cards},
            source_kind=str(payload.get("source_kind") or "manual"),
            source_created_at=payload.get("source_created_at"),
            model=payload.get("model"),
            provider=payload.get("provider"),
            profile=payload.get("profile"),
            workdir=payload.get("workdir"),
        )
        runs.append(run)
    combined_cards = [card for run in runs for card in run.get("cards", [])]
    return {
        "run_id": base_run_id or (runs[0]["id"] if runs else None),
        "runs": runs,
        "cards": combined_cards,
        "upserted": len(combined_cards),
    }


def list_runs(*, project: Optional[str] = None, prong: Optional[str] = None, limit: int = 50) -> list[dict[str, Any]]:
    init_db()
    query = "SELECT * FROM proposal_runs WHERE 1=1"
    params: list[Any] = []
    if project:
        query += " AND project_slug = ?"
        params.append(project)
    if prong:
        query += " AND prong_slug = ?"
        params.append(prong)
    query += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(max(1, min(int(limit or 50), 200)))
    conn = connect()
    try:
        return [_row_to_run(row) for row in conn.execute(query, params).fetchall()]
    finally:
        conn.close()


def get_run(run_id: str, *, include_cards: bool = False) -> dict[str, Any]:
    init_db()
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM proposal_runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        data = _row_to_run(row)
        if include_cards:
            rows = conn.execute(
                "SELECT * FROM proposal_cards WHERE run_id = ? ORDER BY created_at ASC, id ASC",
                (run_id,),
            ).fetchall()
            data["cards"] = [_row_to_card(r) for r in rows]
        return data
    finally:
        conn.close()


def list_proposals(
    *,
    project: Optional[str] = None,
    prong: Optional[str] = None,
    run_id: Optional[str] = None,
    status: Optional[str] = None,
    include_archived: bool = False,
    include_rejected: bool = False,
    limit: int = 200,
) -> list[dict[str, Any]]:
    init_db()
    query = "SELECT * FROM proposal_cards WHERE 1=1"
    params: list[Any] = []
    if project:
        query += " AND project_slug = ?"
        params.append(project)
    if prong:
        query += " AND prong_slug = ?"
        params.append(prong)
    if run_id:
        query += " AND run_id = ?"
        params.append(run_id)
    if status:
        query += " AND status = ?"
        params.append(_normalize_status(status))
    elif not (include_archived or include_rejected):
        placeholders = ",".join("?" for _ in DEFAULT_VISIBLE_STATUSES)
        query += f" AND status IN ({placeholders})"
        params.extend(sorted(DEFAULT_VISIBLE_STATUSES))
    elif include_rejected and not include_archived:
        placeholders = ",".join("?" for _ in (DEFAULT_VISIBLE_STATUSES | {"rejected"}))
        query += f" AND status IN ({placeholders})"
        params.extend(sorted(DEFAULT_VISIBLE_STATUSES | {"rejected"}))
    query += " ORDER BY priority DESC, created_at DESC, id DESC LIMIT ?"
    params.append(max(1, min(int(limit or 200), 500)))
    conn = connect()
    try:
        return [_row_to_card(row) for row in conn.execute(query, params).fetchall()]
    finally:
        conn.close()


def get_proposal(proposal_id: str, *, include_feedback: bool = False, include_run: bool = False) -> dict[str, Any]:
    init_db()
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM proposal_cards WHERE id = ?", (proposal_id,)).fetchone()
        if row is None:
            raise KeyError(proposal_id)
        data = _row_to_card(row)
        if include_feedback:
            rows = conn.execute(
                "SELECT * FROM proposal_feedback WHERE proposal_id = ? ORDER BY created_at DESC, id DESC",
                (proposal_id,),
            ).fetchall()
            data["feedback"] = [_row_to_feedback(r) for r in rows]
        if include_run:
            run = conn.execute("SELECT * FROM proposal_runs WHERE id = ?", (data["run_id"],)).fetchone()
            data["run"] = _row_to_run(run) if run else None
        return data
    finally:
        conn.close()


def _record_feedback(
    conn: sqlite3.Connection,
    *,
    proposal_id: str,
    run_id: str,
    project_slug: str,
    prong_slug: str,
    decision: str,
    reason: Optional[str],
    operator: Optional[str],
    strength: Optional[str] = None,
    kanban_task_id: Optional[str] = None,
) -> None:
    conn.execute(
        """
        INSERT INTO proposal_feedback (
            proposal_id, run_id, project_slug, prong_slug, decision, strength,
            reason, operator, created_at, kanban_task_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            proposal_id,
            run_id,
            project_slug,
            prong_slug,
            decision,
            strength or "moderate",
            _truncate(_redact(reason), 2000),
            operator,
            _now(),
            kanban_task_id,
        ),
    )


def _task_body(card: dict[str, Any]) -> str:
    evidence = card.get("evidence") or []
    criteria = card.get("acceptance_criteria") or []
    sections = [
        card.get("worker_prompt") or card.get("recommended_action") or card.get("title") or "",
        "\n\n## Proposal context",
        card.get("body") or card.get("summary") or "",
    ]
    if evidence:
        sections.append("\n\n## Evidence\n" + "\n".join(f"- {item}" for item in evidence))
    if criteria:
        sections.append("\n\n## Acceptance criteria\n" + "\n".join(f"- {item}" for item in criteria))
    if card.get("risk_notes"):
        sections.append("\n\n## Risk notes\n" + str(card["risk_notes"]))
    sections.append(f"\n\n## Source proposal\n- Proposal ID: `{card['id']}`\n- Run ID: `{card['run_id']}`")
    return "".join(sections).strip()


def _task_public_url(board: str, task_id: str) -> str:
    return f"/workers?board={quote(board)}&task={quote(task_id)}"


def approve_proposal(
    proposal_id: str,
    *,
    operator: Optional[str] = None,
    reason: Optional[str] = None,
    note: Optional[str] = None,
    assignee: Optional[str] = None,
    board: Optional[str] = None,
) -> dict[str, Any]:
    """Approve one proposal and enqueue exactly one Kanban task.

    This function is idempotent at both layers: a decided proposal returns its
    existing task, and Kanban also de-duplicates by ``self-improvement:<id>``.
    """
    init_db()
    if reason is None:
        reason = note
    card = get_proposal(proposal_id)
    if card["status"] in {"approved", "enqueued", "running", "done", "blocked", "failed"} and card.get("kanban_task_id"):
        return get_proposal(proposal_id, include_feedback=True, include_run=True)
    if card["status"] not in {"proposed", "approved"}:
        raise ProposalError(f"proposal {proposal_id} is {card['status']} and cannot be approved")

    project = get_project(card["project_slug"])
    _ = get_prong(project, card["prong_slug"])
    board = str(board or card.get("kanban_board") or project.kanban_board).strip()
    kanban_db.create_board(board, name=f"{project.name} Self-Improvement")
    kanban_db.init_db(board=board)
    kconn = kanban_db.connect(board=board)
    try:
        task_id = kanban_db.create_task(
            kconn,
            title=card["title"],
            body=_task_body(card),
            assignee=assignee or card.get("suggested_assignee") or project.default_assignee,
            created_by="self-improvement-dashboard",
            workspace_kind=card.get("workspace_kind") or project.workspace_kind or "scratch",
            workspace_path=card.get("workspace_path") or project.workspace_path,
            tenant=project.slug,
            priority=int(card.get("priority") or 0),
            idempotency_key=card.get("idempotency_key") or f"self-improvement:{proposal_id}",
            skills=card.get("suggested_skills") or project.default_skills,
            board=board,
        )
    finally:
        kconn.close()

    public_url = _task_public_url(board, task_id)
    now = _now()
    conn = connect()
    try:
        with conn:
            conn.execute(
                """
                UPDATE proposal_cards
                   SET status = 'enqueued', kanban_board = ?, kanban_task_id = ?,
                       worker_public_url = ?, updated_at = ?, decided_at = COALESCE(decided_at, ?),
                       decided_by = COALESCE(decided_by, ?), decision_reason = COALESCE(decision_reason, ?)
                 WHERE id = ?
                """,
                (board, task_id, public_url, now, now, operator, _truncate(_redact(reason), 2000), proposal_id),
            )
            _record_feedback(
                conn,
                proposal_id=proposal_id,
                run_id=card["run_id"],
                project_slug=card["project_slug"],
                prong_slug=card["prong_slug"],
                decision="approved",
                reason=reason,
                operator=operator,
                kanban_task_id=task_id,
            )
    finally:
        conn.close()

    try:
        from hermes_cli.discord_worker_boards import mark_dispatch_dirty

        mark_dispatch_dirty(board=board, reason="self-improvement-approval")
    except Exception:
        pass
    return get_proposal(proposal_id, include_feedback=True, include_run=True)


def reject_proposal(
    proposal_id: str,
    *,
    operator: Optional[str] = None,
    reason: Optional[str] = None,
    feedback: Optional[str] = None,
    strength: Optional[str] = None,
) -> dict[str, Any]:
    init_db()
    if reason is None:
        reason = feedback
    card = get_proposal(proposal_id)
    if card["status"] not in {"proposed", "approved", "rejected"}:
        raise ProposalError(f"proposal {proposal_id} is {card['status']} and cannot be rejected")
    now = _now()
    conn = connect()
    try:
        with conn:
            conn.execute(
                """
                UPDATE proposal_cards
                   SET status = 'rejected', updated_at = ?, decided_at = COALESCE(decided_at, ?),
                       decided_by = COALESCE(decided_by, ?), decision_reason = ?
                 WHERE id = ?
                """,
                (now, now, operator, _truncate(_redact(reason), 2000), proposal_id),
            )
            _record_feedback(
                conn,
                proposal_id=proposal_id,
                run_id=card["run_id"],
                project_slug=card["project_slug"],
                prong_slug=card["prong_slug"],
                decision="rejected",
                reason=reason,
                operator=operator,
                strength=strength,
            )
    finally:
        conn.close()
    return get_proposal(proposal_id, include_feedback=True, include_run=True)


def add_feedback(
    proposal_id: str,
    *,
    feedback: str,
    kind: str = "comment",
    created_by: Optional[str] = None,
    strength: Optional[str] = None,
) -> dict[str, Any]:
    """Attach operator feedback without changing proposal status."""
    init_db()
    card = get_proposal(proposal_id)
    decision = str(kind or "comment").strip().lower() or "comment"
    if not feedback or not str(feedback).strip():
        raise ProposalError("feedback is required")
    conn = connect()
    try:
        with conn:
            _record_feedback(
                conn,
                proposal_id=proposal_id,
                run_id=card["run_id"],
                project_slug=card["project_slug"],
                prong_slug=card["prong_slug"],
                decision=decision,
                reason=feedback,
                operator=created_by,
                strength=strength,
                kanban_task_id=card.get("kanban_task_id"),
            )
    finally:
        conn.close()
    return get_proposal(proposal_id, include_feedback=True, include_run=True)


def feedback_context(*, project: str, prong: Optional[str] = None, limit: int = 12) -> dict[str, Any]:
    init_db()
    query = "SELECT * FROM proposal_feedback WHERE project_slug = ?"
    params: list[Any] = [project]
    if prong:
        query += " AND prong_slug = ?"
        params.append(prong)
    query += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(max(1, min(int(limit or 12), 50)))
    conn = connect()
    try:
        rows = [_row_to_feedback(r) for r in conn.execute(query, params).fetchall()]
    finally:
        conn.close()
    approved = [r for r in rows if r["decision"].startswith("approved")]
    rejected = [r for r in rows if r["decision"].startswith("rejected")]
    lines = [f"Self-improvement feedback context for {project}" + (f"/{prong}" if prong else "")]
    if approved:
        lines.append("\nRecent approved:")
        for item in approved[:6]:
            suffix = f" — {item['reason']}" if item.get("reason") else ""
            lines.append(f"- {item['proposal_id']}{suffix}")
    if rejected:
        lines.append("\nRecent rejected:")
        for item in rejected[:6]:
            suffix = f" — {item['reason']}" if item.get("reason") else ""
            lines.append(f"- {item['proposal_id']}{suffix}")
    if len(lines) == 1:
        lines.append("\nNo operator feedback recorded yet.")
    return {"project": project, "prong": prong, "items": rows, "context": "\n".join(lines)}


# Initialize lazily on import in dashboard contexts; errors should surface at
# request time with a useful message, not fail Hermes import globally.
