"""Sligo dashboard plugin backend API routes."""

from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field

from hermes_cli import kanban_db
from hermes_cli import self_improvement_proposals as proposals
from hermes_cli.config import load_config

router = APIRouter()


class ApproveBody(BaseModel):
    reason: str = ""
    feedback: str = ""


class RejectBody(BaseModel):
    reason: str = ""
    strength: str = ""
    feedback: str = ""


class FeedbackBody(BaseModel):
    feedback: str
    reason: str = ""


class BulkApproveBody(BaseModel):
    proposal_ids: list[int] = Field(default_factory=list)
    confirm: str = ""


def _config() -> dict[str, Any]:
    return load_config()


def _proposal_config(config: dict[str, Any]) -> dict[str, Any]:
    value = config.get("self_improvement", {}).get("proposals", {})
    return value if isinstance(value, dict) else {}


def _operator(request: Request) -> str:
    operator = request.headers.get("X-Hermes-Operator", "")
    if operator and operator.strip():
        return operator.strip()
    session = getattr(request.state, "session", None)
    user = getattr(session, "user", None)
    if user:
        return str(user)
    return "dashboard"


def _resolve(project: str, prong: str | None, config: dict[str, Any]) -> dict[str, Any]:
    try:
        return proposals.resolve_project_prong(project, prong, config=config)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _validate_board(board: str) -> str:
    try:
        normed = kanban_db._normalize_board_slug(board)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not normed:
        raise HTTPException(status_code=400, detail="kanban_board is required")
    if normed != kanban_db.DEFAULT_BOARD and not kanban_db.board_exists(normed):
        raise HTTPException(status_code=404, detail=f"board {normed!r} does not exist")
    return normed


def _project_payload(key: str, cfg: dict[str, Any]) -> dict[str, Any]:
    prongs = cfg.get("prongs", {}) or {}
    return {
        "key": key,
        "name": cfg.get("name") or key,
        "description": cfg.get("description") or "",
        "default_prong": cfg.get("default_prong") or "",
        "prong_aliases": cfg.get("prong_aliases") or {},
        "prongs": [
            {
                "key": prong_key,
                "name": prong_cfg.get("name") or prong_key,
                "description": prong_cfg.get("description") or "",
                "cron_job_ids": prong_cfg.get("cron_job_ids") or [],
            }
            for prong_key, prong_cfg in prongs.items()
            if isinstance(prong_cfg, dict)
        ],
    }


def _card_with_links(card: dict[str, Any]) -> dict[str, Any]:
    item = dict(card)
    if item.get("source_output_ref"):
        item["source_output_url"] = f"/api/plugins/sligo/source-output/{item['source_output_ref']}"
    item["worker"] = {
        "board": card.get("worker_board") or "",
        "task_id": card.get("worker_task_id") or "",
        "status": card.get("worker_status") or "",
        "url": card.get("worker_url") or "",
    }
    return item


def _run_response(run: dict[str, Any]) -> dict[str, Any]:
    if run.get("source_output_ref"):
        run = dict(run)
        run["source_output_url"] = f"/api/plugins/sligo/source-output/{run['source_output_ref']}"
    return {"run": run}


def _run_with_source_link(run: dict[str, Any]) -> dict[str, Any]:
    return _run_response(run)["run"]


def _card_response(card: dict[str, Any]) -> dict[str, Any]:
    return {"proposal": _card_with_links(card)}


def _worker_body(card: dict[str, Any], resolved: dict[str, Any], prong_cfg: dict[str, Any]) -> str:
    prompt = card.get("worker_prompt") or card.get("proposed_worker_prompt") or prong_cfg.get("worker_prompt") or resolved["project"].get("worker_prompt")
    parts = [
        prompt or "Implement the approved Sligo proposal.",
        "",
        f"Proposal: {card['title']}",
    ]
    for label, field in (
        ("Summary", "summary"),
        ("Body", "body"),
        ("Rationale", "rationale"),
        ("Expected outcome", "expected_outcome"),
    ):
        if card.get(field):
            parts.extend(["", f"{label}:\n{card[field]}"])
    evidence = card.get("evidence_bullets") or []
    if evidence:
        parts.extend(["", "Evidence:"])
        parts.extend(f"- {item}" for item in evidence if item)
    card_acceptance = card.get("acceptance_criteria") or []
    if card_acceptance:
        parts.extend(["", "Proposal acceptance criteria:"])
        parts.extend(f"- {item}" for item in card_acceptance if item)
    acceptance = prong_cfg.get("acceptance_criteria") or resolved["project"].get("acceptance_criteria") or []
    if isinstance(acceptance, str):
        acceptance = [acceptance]
    if acceptance:
        parts.extend(["", "Acceptance criteria:"])
        parts.extend(f"- {item}" for item in acceptance if item)
    if card.get("source_url"):
        parts.extend(["", f"Source: {card['source_url']}"])
    return "\n".join(parts).strip()


def _priority(value: str) -> int:
    lookup = {"low": -1, "normal": 0, "medium": 0, "high": 1, "urgent": 2}
    return lookup.get((value or "").strip().lower(), 0)


def _worker_url(board: str, task_id: str, prong_cfg: dict[str, Any], project_cfg: dict[str, Any]) -> str:
    template = prong_cfg.get("worker_url_template") or project_cfg.get("worker_url_template") or ""
    if template:
        return str(template).format(board=board, task_id=task_id)
    return f"/kanban?board={board}&task={task_id}"


def _resolve_workspace_path(workspace_kind: str, workspace_path: Any) -> str | None:
    if workspace_kind not in kanban_db.VALID_WORKSPACE_KINDS:
        raise HTTPException(status_code=400, detail=f"invalid workspace_kind: {workspace_kind}")
    if workspace_kind == "scratch":
        if workspace_path:
            raise HTTPException(status_code=400, detail="workspace_path is not allowed for scratch workspaces")
        return None
    if not workspace_path:
        raise HTTPException(status_code=400, detail="configured workspace_path is required for persistent workspaces")
    try:
        resolved = Path(str(workspace_path)).expanduser().resolve(strict=True)
    except OSError as exc:
        raise HTTPException(status_code=400, detail="configured workspace_path must exist") from exc
    if not resolved.is_dir():
        raise HTTPException(status_code=400, detail="configured workspace_path must be a directory")
    return str(resolved)


@router.get("/projects")
def list_projects() -> dict[str, Any]:
    proposal_cfg = _proposal_config(_config())
    projects = proposal_cfg.get("projects", {}) or {}
    return {
        "projects": [_project_payload(key, cfg) for key, cfg in projects.items() if isinstance(cfg, dict)],
        "project_aliases": proposal_cfg.get("project_aliases", {}) or {},
    }


@router.get("/proposals")
def list_proposals(
    project: str | None = None,
    prong: str | None = None,
    status_filter: str | None = Query(default="proposed", alias="status"),
    include_inactive: bool = False,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    if project:
        resolved = _resolve(project, prong, _config())
        project_key = resolved["project_key"]
        prong_key = resolved["prong_key"] if prong else None
    else:
        project_key = None
        prong_key = None
    status_value = None if include_inactive else status_filter
    cards = proposals.list_cards(project_key=project_key, prong_key=prong_key, status=status_value, limit=limit)
    return {"proposals": [_card_with_links(card) for card in cards]}


@router.get("/proposals/{proposal_id}")
def get_proposal(proposal_id: int) -> dict[str, Any]:
    try:
        card = proposals.get_card(proposal_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _card_response(card)


@router.get("/runs")
def list_runs(
    project: str | None = None,
    prong: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    with proposals.connect() as conn:
        clauses: list[str] = []
        params: list[Any] = []
        if project:
            resolved = _resolve(project, prong, _config())
            clauses.append("project_key = ?")
            params.append(resolved["project_key"])
            if prong:
                clauses.append("prong_key = ?")
                params.append(resolved["prong_key"])
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = conn.execute(
            f"SELECT * FROM proposal_runs{where} ORDER BY created_at DESC, id DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        runs = [_run_with_source_link(proposals.sanitize_run(proposals.decode_row(row))) for row in rows]
    return {"runs": runs}


@router.get("/runs/{run_id}")
def get_run(run_id: int) -> dict[str, Any]:
    try:
        run = proposals.get_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    cards = proposals.list_cards(run_id=run_id, limit=200)
    return {"run": _run_with_source_link(run), "proposals": [_card_with_links(card) for card in cards]}


@router.get("/source-output/{ref}")
def get_source_output(ref: str) -> Response:
    try:
        path = proposals.resolve_source_output_ref(ref)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="source output file not found")
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=415, detail="source output file is not UTF-8 markdown") from None
    except OSError as exc:
        raise HTTPException(status_code=404, detail="source output file not found") from exc
    return Response(content, media_type="text/markdown; charset=utf-8")


@router.post("/proposals/{proposal_id}/approve")
def approve_proposal(proposal_id: int, body: ApproveBody, request: Request) -> dict[str, Any]:
    actor = _operator(request)
    try:
        with proposals.connect() as conn:
            card = proposals.get_card(proposal_id, conn=conn, public=False)
            if card.get("status") == "approved" and card.get("worker_task_id"):
                return _card_response(proposals.sanitize_card(card))

            config = _config()
            resolved = _resolve(card["project_key"], card["prong_key"], config)
            project_cfg = resolved["project"]
            prong_cfg = resolved["prong"]
            board = _validate_board(str(prong_cfg.get("kanban_board") or project_cfg.get("kanban_board") or ""))
            workspace_kind = str(prong_cfg.get("workspace_kind") or project_cfg.get("workspace_kind") or "dir")
            workspace_path = prong_cfg.get("workspace_path") or project_cfg.get("workspace_path")
            resolved_workspace_path = _resolve_workspace_path(workspace_kind, workspace_path)
            if card.get("worker_task_id"):
                task_id = card["worker_task_id"]
            else:
                initial_status = str(prong_cfg.get("initial_status") or project_cfg.get("initial_status") or "blocked")
                target_status = str(prong_cfg.get("target_status") or project_cfg.get("target_status") or "ready")
                with kanban_db.connect_closing(board=board) as kb_conn:
                    task_id = kanban_db.create_task(
                        kb_conn,
                        title=card["title"],
                        body=_worker_body(card, resolved, prong_cfg),
                        assignee=prong_cfg.get("assignee") or project_cfg.get("assignee"),
                        created_by=actor,
                        workspace_kind=workspace_kind,
                        workspace_path=resolved_workspace_path,
                        branch_name=prong_cfg.get("branch_name") or project_cfg.get("branch_name"),
                        tenant=prong_cfg.get("tenant") or project_cfg.get("tenant"),
                        priority=_priority(card.get("priority", "")),
                        idempotency_key=f"self-improvement:{proposal_id}",
                        max_runtime_seconds=prong_cfg.get("max_runtime_seconds") or project_cfg.get("max_runtime_seconds"),
                        skills=prong_cfg.get("skills") or project_cfg.get("skills"),
                        initial_status=initial_status,
                        board=board,
                    )
                    if target_status and target_status != initial_status:
                        kanban_db.move_task_status(kb_conn, task_id, target_status, source="sligo/approve")
            worker_status = locals().get("target_status") or card.get("worker_status") or "ready"
            url = card.get("worker_url") or _worker_url(board, task_id, prong_cfg, project_cfg)
            linked = proposals.link_worker_task(proposal_id, board=board, task_id=task_id, status=worker_status, url=url, conn=conn)
            decided = proposals.record_decision(
                proposal_id,
                "approved",
                actor=actor,
                reason=body.reason,
                feedback=body.feedback,
                conn=conn,
            )
            decided.update({k: linked[k] for k in ("worker_board", "worker_task_id", "worker_status", "worker_url")})
            return _card_response(proposals.sanitize_card(decided))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/proposals/{proposal_id}/reject")
def reject_proposal(proposal_id: int, body: RejectBody, request: Request) -> dict[str, Any]:
    note = body.reason
    if body.strength:
        note = f"{note} [strength={body.strength}]" if note else f"strength={body.strength}"
    try:
        card = proposals.record_decision(
            proposal_id,
            "rejected",
            actor=_operator(request),
            reason=note,
            feedback=body.feedback,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _card_response(proposals.sanitize_card(card))


@router.post("/proposals/{proposal_id}/feedback")
def record_feedback(proposal_id: int, body: FeedbackBody, request: Request) -> dict[str, Any]:
    try:
        with proposals.connect() as conn:
            card = proposals.get_card(proposal_id, conn=conn, public=False)
            now = proposals.utc_now()
            audit = card.get("audit_log", []) + [proposals.audit_event("feedback", _operator(request), body.reason or body.feedback)]
            proposals.record_feedback_event(
                proposal_id,
                action="feedback",
                actor=_operator(request),
                reason=body.reason,
                feedback=body.feedback,
                conn=conn,
            )
            conn.execute(
                """
                UPDATE proposal_cards
                SET operator_feedback = ?, decision_reason = COALESCE(NULLIF(?, ''), decision_reason), audit_log = ?, updated_at = ?
                WHERE id = ?
                """,
                (body.feedback, body.reason, proposals.json_text(audit), now, proposal_id),
            )
            return _card_response(proposals.get_card(proposal_id, conn=conn))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/proposals/bulk-approve")
def bulk_approve(body: BulkApproveBody) -> dict[str, Any]:
    if body.confirm != "APPROVE":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="bulk approval requires confirm='APPROVE' and is not implemented by this endpoint yet",
        )
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="bulk approval is not implemented")


@router.get("/worker-tasks/{board}/{task_id}")
def get_worker_task(board: str, task_id: str) -> dict[str, Any]:
    board_slug = _validate_board(board)
    with kanban_db.connect_closing(board=board_slug) as conn:
        task = kanban_db.get_task(conn, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Unknown worker task: {task_id}")
    return {"board": board_slug, "task": asdict(task)}
