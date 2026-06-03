"""Sligo self-improvement dashboard plugin API.

Mounted at /api/plugins/sligo/ by the dashboard plugin system.  HTTP routes are
protected by the dashboard auth middleware; mutation routes stay POST-only and
delegate all state changes to hermes_cli.self_improvement_proposals.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from hermes_cli import self_improvement_proposals as proposals

router = APIRouter()


class ProposalIngestBody(BaseModel):
    project: Optional[str] = None
    prong: Optional[str] = None
    cron_job_id: Optional[str] = None
    cron_job_name: Optional[str] = None
    cron_output_path: Optional[str] = None
    cron_output_text: str = ""
    proposal_json: Optional[dict[str, Any]] = None
    source_kind: str = "manual"
    source_created_at: Optional[int] = None
    model: Optional[str] = None
    provider: Optional[str] = None
    profile: Optional[str] = None
    workdir: Optional[str] = None


class DecisionBody(BaseModel):
    operator: Optional[str] = None
    reason: Optional[str] = None
    strength: Optional[str] = Field(default=None, description="weak/moderate/strong feedback strength")


class FeedbackBody(BaseModel):
    feedback: str
    kind: str = "comment"
    created_by: Optional[str] = None
    strength: Optional[str] = None


class PatchProposalBody(BaseModel):
    title: Optional[str] = None
    summary: Optional[str] = None
    worker_prompt: Optional[str] = None
    acceptance_criteria: Optional[list[str]] = None


def _domain_error(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


@router.get("/projects")
async def list_projects():
    return {"projects": proposals.list_projects()}


@router.get("/runs")
async def list_runs(
    project: Optional[str] = None,
    prong: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=200),
):
    return {"runs": proposals.list_runs(project=project, prong=prong, limit=limit)}


@router.get("/runs/{run_id}")
async def get_run(run_id: str):
    try:
        return proposals.get_run(run_id, include_cards=True)
    except KeyError:
        raise HTTPException(status_code=404, detail="proposal run not found")


@router.get("/proposals")
async def list_proposals(
    project: Optional[str] = None,
    prong: Optional[str] = None,
    status: Optional[str] = None,
    include_archived: bool = False,
    limit: int = Query(default=200, ge=1, le=500),
):
    try:
        cards = proposals.list_proposals(
            project=project,
            prong=prong,
            status=status,
            include_archived=include_archived,
            limit=limit,
        )
    except proposals.ProposalError as exc:
        raise _domain_error(exc)
    return {"proposals": cards}


@router.get("/proposals/{proposal_id}")
async def get_proposal(proposal_id: str):
    try:
        return proposals.get_proposal(proposal_id, include_feedback=True, include_run=True)
    except KeyError:
        raise HTTPException(status_code=404, detail="proposal not found")


@router.post("/proposals/ingest")
async def ingest(body: ProposalIngestBody):
    try:
        return proposals.ingest_proposals(
            project_slug=body.project,
            prong_slug=body.prong,
            cron_job_id=body.cron_job_id,
            cron_job_name=body.cron_job_name,
            cron_output_path=body.cron_output_path,
            cron_output_text=body.cron_output_text,
            proposal_json=body.proposal_json,
            source_kind=body.source_kind,
            source_created_at=body.source_created_at,
            model=body.model,
            provider=body.provider,
            profile=body.profile,
            workdir=body.workdir,
        )
    except proposals.ProposalError as exc:
        raise _domain_error(exc)


@router.post("/ingest")
async def ingest_flexible(body: dict[str, Any]):
    try:
        return proposals.ingest_run(body)
    except proposals.ProposalError as exc:
        raise _domain_error(exc)


@router.post("/proposals/{proposal_id}/approve")
async def approve(proposal_id: str, body: DecisionBody):
    try:
        return proposals.approve_proposal(proposal_id, operator=body.operator, reason=body.reason)
    except KeyError:
        raise HTTPException(status_code=404, detail="proposal not found")
    except proposals.ProposalError as exc:
        raise _domain_error(exc)


@router.post("/proposals/{proposal_id}/reject")
async def reject(proposal_id: str, body: DecisionBody):
    try:
        return proposals.reject_proposal(
            proposal_id,
            operator=body.operator,
            reason=body.reason,
            strength=body.strength,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="proposal not found")
    except proposals.ProposalError as exc:
        raise _domain_error(exc)


@router.post("/proposals/{proposal_id}/feedback")
async def add_feedback(proposal_id: str, body: FeedbackBody):
    try:
        return proposals.add_feedback(
            proposal_id,
            feedback=body.feedback,
            kind=body.kind,
            created_by=body.created_by,
            strength=body.strength,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="proposal not found")
    except proposals.ProposalError as exc:
        raise _domain_error(exc)


@router.get("/feedback-context")
async def feedback_context(
    project: str,
    prong: Optional[str] = None,
    limit: int = Query(default=12, ge=1, le=50),
):
    try:
        return proposals.feedback_context(project=project, prong=prong, limit=limit)
    except proposals.ProposalError as exc:
        raise _domain_error(exc)
