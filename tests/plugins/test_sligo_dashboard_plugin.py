from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb
from hermes_cli import self_improvement_proposals as sip


def _load_router():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "sligo" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("hermes_dashboard_plugin_sligo_test", plugin_file)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.router


def _config(tmp_path: Path) -> dict:
    return {
        "self_improvement": {
            "enabled": True,
            "storage_db": str(tmp_path / "proposals.db"),
            "projects": {
                "sligo": {
                    "name": "Sligo",
                    "workspace_path": str(tmp_path / "sligo"),
                    "board": "sligo-board",
                    "assignee": "dev",
                    "skills": [],
                    "prongs": {"airflow_doctor": {"name": "Airflow Doctor", "enabled": True}},
                }
            },
        }
    }


def _payload() -> dict:
    return {
        "hermes_self_improvement_proposals_version": 1,
        "project": "sligo",
        "prong": "airflow_doctor",
        "proposals": [
            {
                "title": "Add DAG health summary",
                "summary": "Expose failed DAGs.",
                "body": "Operators need this in the admin view.",
                "evidence": [],
                "worker_prompt": "Implement a read-only failed DAG summary.",
                "acceptance_criteria": ["Failed DAGs are visible without credentials."],
                "priority": "high",
                "confidence": 0.8,
                "effort": "small",
            }
        ],
    }


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    workspace = tmp_path / "sligo"
    workspace.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    sip._INITIALIZED_PATHS.clear()
    cfg = _config(tmp_path)
    monkeypatch.setattr(sip, "load_config", lambda: cfg)
    return cfg


@pytest.fixture
def client(isolated):
    app = FastAPI()

    @app.middleware("http")
    async def require_test_token(request, call_next):
        if request.url.path.startswith("/api/plugins/sligo") and request.headers.get("X-Hermes-Session-Token") != "test-token":
            from fastapi.responses import JSONResponse

            return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
        return await call_next(request)

    app.include_router(_load_router(), prefix="/api/plugins/sligo")
    return TestClient(app)


def _ingest_card(client: TestClient) -> str:
    r = client.post(
        "/api/plugins/sligo/ingest",
        headers={"X-Hermes-Session-Token": "test-token"},
        json={"metadata": {"proposal_json": json.dumps(_payload()), "cron_run_id": "run-1"}},
    )
    assert r.status_code == 200
    return r.json()["card_ids"][0]


def test_unauthenticated_mutations_fail(client):
    card_id = _ingest_card(client)

    assert client.post(f"/api/plugins/sligo/proposals/{card_id}/approve").status_code == 401
    assert client.post(f"/api/plugins/sligo/proposals/{card_id}/reject", json={}).status_code == 401


def test_approve_creates_one_idempotent_kanban_task(client, isolated):
    card_id = _ingest_card(client)
    headers = {"X-Hermes-Session-Token": "test-token"}

    first = client.post(f"/api/plugins/sligo/proposals/{card_id}/approve", headers=headers)
    second = client.post(f"/api/plugins/sligo/proposals/{card_id}/approve", headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    first_task = first.json()["proposal"]["linked_kanban_task_id"]
    assert second.json()["proposal"]["linked_kanban_task_id"] == first_task
    assert second.json()["proposal"]["worker"]["url"]
    with kb.connect(board="sligo-board") as conn:
        rows = conn.execute("SELECT * FROM tasks WHERE idempotency_key = ?", (f"self-improvement:{card_id}",)).fetchall()
    assert len(rows) == 1
    assert rows[0]["workspace_path"] == isolated["self_improvement"]["projects"]["sligo"]["workspace_path"]
    assert "Worker prompt:" in rows[0]["body"]


def test_reject_records_feedback_without_task(client):
    card_id = _ingest_card(client)
    headers = {"X-Hermes-Session-Token": "test-token"}

    r = client.post(
        f"/api/plugins/sligo/proposals/{card_id}/reject",
        headers=headers,
        json={"reason": "Not valuable", "strength": "strong"},
    )

    assert r.status_code == 200
    assert r.json()["proposal"]["status"] == "rejected"
    feedback = sip.list_feedback(card_id)
    assert feedback[0]["feedback_type"] == "reject"
    assert feedback[0]["body"] == "Not valuable"
    with kb.connect(board="sligo-board") as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_parse_error_run_is_visible_without_proposal_cards(client):
    r = client.post(
        "/api/plugins/sligo/ingest",
        headers={"X-Hermes-Session-Token": "test-token"},
        json={
            "metadata": {
                "proposal_json": "{not json",
                "cron_run_id": "bad-run",
                "project": "sligo",
                "prong": "airflow_doctor",
                "cron_output_path": "/tmp/cron-output.txt",
                "cron_output_sha256": "abc123",
                "source_timestamp": 1700000000,
            }
        },
    )

    assert r.status_code == 200
    assert r.json()["parse_status"] == "parse_error"
    headers = {"X-Hermes-Session-Token": "test-token"}
    runs = client.get("/api/plugins/sligo/runs?parse_status=parse_error", headers=headers).json()["runs"]
    assert runs[0]["project"] == "sligo"
    assert runs[0]["prong"] == "airflow_doctor"
    assert runs[0]["cron_output_path"] == "/tmp/cron-output.txt"
    assert runs[0]["cron_output_sha256"] == "abc123"
    assert runs[0]["source_timestamp"] == 1700000000
    assert "Malformed proposal JSON" in runs[0]["parse_error"]
    detail = client.get(f"/api/plugins/sligo/runs/{runs[0]['id']}", headers=headers).json()["run"]
    assert detail["parse_error"] == runs[0]["parse_error"]
    assert client.get("/api/plugins/sligo/proposals", headers=headers).json()["proposals"] == []


def test_limited_edit_validation_and_unsafe_override_rejection(client):
    card_id = _ingest_card(client)
    headers = {"X-Hermes-Session-Token": "test-token"}

    r = client.patch(f"/api/plugins/sligo/proposals/{card_id}", headers=headers, json={"priority": "low"})
    assert r.status_code == 200
    assert r.json()["proposal"]["priority"] == "low"

    bad = client.patch(f"/api/plugins/sligo/proposals/{card_id}", headers=headers, json={"workspace_path": "/tmp/evil"})
    assert bad.status_code == 422


def test_dashboard_edit_sanitizes_visible_fields_in_response_and_storage(client, isolated):
    card_id = _ingest_card(client)
    headers = {"X-Hermes-Session-Token": "test-token"}

    r = client.patch(
        f"/api/plugins/sligo/proposals/{card_id}",
        headers=headers,
        json={
            "title": "Edited sk-proj-dashboardTitleSecret1234567890",
            "summary": "Edited OPENAI_API_KEY=dashboardSummarySecret1234567890",
            "body": "Edited body ghp_dashboardBodySecret1234567890",
            "acceptance_criteria": ["Hide sk-proj-dashboardCriteriaSecret1234567890"],
        },
    )

    assert r.status_code == 200
    returned = json.dumps(r.json()["proposal"], sort_keys=True)
    assert "sk-proj-dashboardTitleSecret1234567890" not in returned
    assert "dashboardSummarySecret1234567890" not in returned
    assert "ghp_dashboardBodySecret1234567890" not in returned
    assert "sk-proj-dashboardCriteriaSecret1234567890" not in returned
    with sip.connect(config=isolated) as conn:
        row = conn.execute(
            "SELECT title, summary, body, acceptance_criteria_json FROM proposal_cards WHERE card_id = ?",
            (card_id,),
        ).fetchone()
    stored = " ".join(str(value) for value in row)
    assert "sk-proj-dashboardTitleSecret1234567890" not in stored
    assert "dashboardSummarySecret1234567890" not in stored
    assert "ghp_dashboardBodySecret1234567890" not in stored
    assert "sk-proj-dashboardCriteriaSecret1234567890" not in stored


def test_approve_rejects_unsafe_project_workspace(client, isolated):
    card_id = _ingest_card(client)
    with sip.connect(config=isolated) as conn:
        conn.execute("UPDATE proposal_cards SET resolved_workspace_path = ? WHERE card_id = ?", ("/tmp/evil", card_id))
        conn.commit()

    r = client.post(
        f"/api/plugins/sligo/proposals/{card_id}/approve",
        headers={"X-Hermes-Session-Token": "test-token"},
    )

    assert r.status_code == 400
    assert "Workspace does not match" in r.json()["detail"]
