"""Sligo self-improvement dashboard plugin API."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from hermes_cli import self_improvement_proposals as sip


router = APIRouter()


class IngestBody(BaseModel):
    metadata: dict[str, Any] = Field(default_factory=dict)
    output_text: Optional[str] = None


class FeedbackBody(BaseModel):
    feedback_type: str = "comment"
    body: str
    author: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RejectBody(BaseModel):
    reason: Optional[str] = None
    strength: Optional[str] = None
    actor: Optional[str] = None


class EditBody(BaseModel):
    title: Optional[str] = None
    summary: Optional[str] = None
    body: Optional[str] = None
    priority: Optional[str] = None
    confidence: Optional[float] = None
    effort: Optional[str] = None
    acceptance_criteria: Optional[list[str]] = None
    project: Optional[str] = None
    prong: Optional[str] = None
    workspace_path: Optional[str] = None
    board: Optional[str] = None
    assignee: Optional[str] = None
    skills: Optional[list[str]] = None
    shell_command: Optional[str] = None

    @model_validator(mode="after")
    def reject_control_plane_overrides(self):
        blocked = [
            name for name in (
                "project", "prong", "workspace_path", "board", "assignee", "skills", "shell_command",
            )
            if getattr(self, name) is not None
        ]
        if blocked:
            raise ValueError(f"unsafe edit field(s): {', '.join(blocked)}")
        return self


@router.get("/projects")
def list_projects():
    return {"projects": sip.list_projects()}


@router.get("/runs")
def list_runs(
    project: Optional[str] = None,
    prong: Optional[str] = None,
    parse_status: Optional[str] = None,
    limit: int = 50,
):
    return {"runs": sip.list_runs(project=project, prong=prong, parse_status=parse_status, limit=limit)}


@router.get("/runs/{run_id}")
def get_run(run_id: int):
    detail = sip.get_run_detail(run_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="run not found")
    return {"run": detail}


@router.get("/proposals")
def list_proposals(
    project: Optional[str] = None,
    prong: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
):
    return {"proposals": sip.list_proposals(project=project, prong=prong, status=status, limit=limit)}


@router.get("/proposals/{card_id}")
def get_proposal(card_id: str):
    detail = sip.get_proposal_detail(card_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="proposal not found")
    detail["feedback"] = sip.list_feedback(card_id)
    return {"proposal": detail}


@router.post("/ingest")
def ingest(body: IngestBody):
    return sip.ingest_proposal_output(metadata=body.metadata, output_text=body.output_text)


@router.post("/proposals/{card_id}/approve")
def approve(card_id: str, request: Request):
    try:
        return {"proposal": sip.approve_proposal(card_id, actor=_actor(request))}
    except KeyError:
        raise HTTPException(status_code=404, detail="proposal not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/proposals/{card_id}/reject")
def reject(card_id: str, body: RejectBody, request: Request):
    try:
        return {"proposal": sip.reject_proposal(card_id, reason=body.reason, strength=body.strength, actor=body.actor or _actor(request))}
    except KeyError:
        raise HTTPException(status_code=404, detail="proposal not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/proposals/{card_id}/feedback")
def add_feedback(card_id: str, body: FeedbackBody, request: Request):
    try:
        feedback = sip.add_feedback(
            card_id,
            feedback_type=body.feedback_type,
            body=body.body,
            author=body.author or _actor(request),
            metadata=body.metadata,
        )
        return {"feedback": feedback}
    except KeyError:
        raise HTTPException(status_code=404, detail="proposal not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.patch("/proposals/{card_id}")
def edit(card_id: str, body: EditBody):
    try:
        return {"proposal": sip.edit_proposal(card_id, **body.model_dump(exclude_none=True))}
    except KeyError:
        raise HTTPException(status_code=404, detail="proposal not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _actor(request: Request) -> str:
    return request.headers.get("X-Hermes-Actor") or "dashboard"
