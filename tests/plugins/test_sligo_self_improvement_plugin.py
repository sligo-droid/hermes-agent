"""Tests for the Sligo self-improvement proposal plugin."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db
from hermes_cli import self_improvement_proposals as proposals


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    proposals.init_db()
    return home


def _payload(title: str = "Fix admin empty state") -> dict:
    return {
        "project": "pid",
        "prong": "visible-ux",
        "cards": [
            {
                "title": title,
                "summary": "A generated proposal that should become a card.",
                "evidence": ["Source: dashboard smoke", "No credentials should appear"],
                "recommended_action": "Improve the empty state copy.",
                "worker_prompt": "Implement the empty state copy fix and add a smoke test.",
                "acceptance_criteria": ["Empty state renders", "No layout regression"],
                "priority": 2,
                "confidence": 0.84,
                "estimated_effort": "low",
                # This must be ignored; workspace routing comes from trusted project config.
                "workspace_path": "/tmp/attacker-controlled-path",
            }
        ],
    }


def _load_plugin_router():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "sligo" / "dashboard" / "plugin_api.py"
    assert plugin_file.exists(), f"plugin file missing: {plugin_file}"
    spec = importlib.util.spec_from_file_location("hermes_dashboard_plugin_sligo_test", plugin_file)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.router


@pytest.fixture
def client(hermes_home):
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/sligo")
    return TestClient(app)


def test_ingest_strict_payload_creates_run_and_visible_card(hermes_home):
    result = proposals.ingest_proposals(
        cron_job_id="job-visible",
        cron_output_text="human summary",
        proposal_json=_payload(),
    )

    assert result["parse_status"] == "ok"
    assert result["project_slug"] == "pid"
    assert len(result["cards"]) == 1
    card = result["cards"][0]
    assert card["title"] == "Fix admin empty state"
    assert card["status"] == "proposed"
    assert card["workspace_kind"] == "scratch"
    assert card["workspace_path"] is None

    listed = proposals.list_proposals(project="pid", status="proposed")
    assert [c["id"] for c in listed] == [card["id"]]


def test_ingest_records_parse_failure_for_unstructured_markdown(hermes_home):
    result = proposals.ingest_proposals(
        project_slug="pid",
        prong_slug="visible-ux",
        cron_output_text="# Just prose\nNo machine-readable cards here.",
    )

    assert result["parse_status"] == "failed"
    assert "no strict proposal JSON" in result["parse_error"]
    assert result["cards"] == []


def test_plugin_approve_is_idempotent_and_creates_one_kanban_task(client, hermes_home):
    r = client.post(
        "/api/plugins/sligo/proposals/ingest",
        json={"proposal_json": _payload("Approve me"), "source_kind": "test"},
    )
    assert r.status_code == 200, r.text
    card = r.json()["cards"][0]

    first = client.post(
        f"/api/plugins/sligo/proposals/{card['id']}/approve",
        json={"operator": "pytest", "reason": "looks useful"},
    )
    assert first.status_code == 200, first.text
    approved = first.json()
    assert approved["status"] == "enqueued"
    assert approved["kanban_board"] == "pid"
    assert approved["kanban_task_id"]
    assert approved["worker_public_url"].startswith("/kanban?board=pid")

    second = client.post(
        f"/api/plugins/sligo/proposals/{card['id']}/approve",
        json={"operator": "pytest", "reason": "double click"},
    )
    assert second.status_code == 200, second.text
    assert second.json()["kanban_task_id"] == approved["kanban_task_id"]

    conn = kanban_db.connect(board="pid")
    try:
        tasks = kanban_db.list_tasks(conn, tenant="pid", include_archived=True)
    finally:
        conn.close()
    assert len(tasks) == 1
    assert tasks[0].id == approved["kanban_task_id"]
    assert tasks[0].idempotency_key == f"self-improvement:{card['id']}"


def test_plugin_reject_hides_from_default_list_but_feedback_remains(client, hermes_home):
    ingest = client.post(
        "/api/plugins/sligo/proposals/ingest",
        json={"proposal_json": _payload("Reject me"), "source_kind": "test"},
    )
    assert ingest.status_code == 200, ingest.text
    card = ingest.json()["cards"][0]

    rejected = client.post(
        f"/api/plugins/sligo/proposals/{card['id']}/reject",
        json={"operator": "pytest", "reason": "too broad", "strength": "strong"},
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "rejected"
    assert rejected.json()["feedback"][0]["decision"] == "rejected"

    default_list = client.get("/api/plugins/sligo/proposals?project=pid")
    assert default_list.status_code == 200
    assert all(item["id"] != card["id"] for item in default_list.json()["proposals"])

    archived_list = client.get("/api/plugins/sligo/proposals?project=pid&include_archived=true")
    assert archived_list.status_code == 200
    assert any(item["id"] == card["id"] for item in archived_list.json()["proposals"])

    context = client.get("/api/plugins/sligo/feedback-context?project=pid&prong=visible-ux")
    assert context.status_code == 200
    assert "too broad" in context.json()["context"]
