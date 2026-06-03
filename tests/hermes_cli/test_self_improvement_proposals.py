from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db
from hermes_cli import self_improvement_proposals as sip


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return home


def sample_payload():
    return {
        "project": "hermes-agent",
        "run_id": "run-1",
        "source": "meeting-notes",
        "proposals": [
            {
                "id": "proposal-1",
                "prong": "quality",
                "title": "Add focused tests",
                "summary": "Add missing regression tests for the worker path.",
                "rationale": "The current path has no durable check.",
                "priority": 3,
                "source_ref": "meeting:42",
            }
        ],
    }


def test_ingest_run_lists_projects_runs_and_proposals(hermes_home):
    result = sip.ingest_run(sample_payload())

    assert result["run_id"] == "run-1"
    assert result["upserted"] == 1
    assert sip.db_path() == hermes_home / "self_improvement" / "proposals.db"
    projects = sip.list_projects()
    project = next(item for item in projects if item["project"] == "hermes-agent")
    assert project["proposal_count"] == 1
    assert project["pending_count"] == 1
    assert project["updated_at"]
    assert sip.list_runs(project="hermes-agent")[0]["id"] == "run-1"
    proposals = sip.list_proposals(project="hermes-agent")
    assert [p["id"] for p in proposals] == ["proposal-1"]
    assert proposals[0]["status"] == "proposed"
    assert proposals[0]["body"] == "The current path has no durable check."


def test_approve_proposal_creates_idempotent_kanban_task(hermes_home):
    sip.ingest_run(sample_payload())

    first = sip.approve_proposal("proposal-1", note="Looks actionable")
    second = sip.approve_proposal("proposal-1", note="Retry should not duplicate")

    assert first["status"] == "enqueued"
    assert first["task_id"] == second["task_id"]
    with kanban_db.connect(board="hermes-agent") as conn:
        tasks = kanban_db.list_tasks(conn, include_archived=True)
    assert len(tasks) == 1
    assert tasks[0].id == first["task_id"]
    assert tasks[0].created_by == "self-improvement-dashboard"
    assert tasks[0].idempotency_key == "self-improvement:proposal-1"
    assert tasks[0].tenant == "hermes-agent"
    assert "Proposal ID: `proposal-1`" in (tasks[0].body or "")


def test_reject_proposal_persists_feedback_and_hides_by_default(hermes_home):
    sip.ingest_run(sample_payload())

    rejected = sip.reject_proposal("proposal-1", feedback="Too vague")

    assert rejected["status"] == "rejected"
    assert rejected["feedback"][0]["feedback"] == "Too vague"
    assert sip.list_proposals() == []
    assert sip.list_proposals(include_rejected=True)[0]["id"] == "proposal-1"


def test_add_feedback_preserves_proposal_status(hermes_home):
    sip.ingest_run(sample_payload())

    updated = sip.add_feedback("proposal-1", feedback="Needs owner", kind="comment", created_by="tester")

    assert updated["status"] == "proposed"
    assert updated["feedback"][0]["kind"] == "comment"
    assert updated["feedback"][0]["created_by"] == "tester"


def test_ingest_accepts_cards_alias(hermes_home):
    result = sip.ingest_run(
        {
            "project": "hermes-agent",
            "run_id": "run-cards",
            "cards": [{"id": "card-1", "title": "Cards alias", "area": "api"}],
        }
    )

    assert result["upserted"] == 1
    proposal = sip.get_proposal("card-1")
    assert proposal is not None
    assert proposal["prong"] == "api"
