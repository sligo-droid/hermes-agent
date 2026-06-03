"""Tests for the Sligo dashboard plugin backend."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb
from hermes_cli import self_improvement_proposals as proposals


def _load_plugin_module():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "sligo" / "dashboard" / "plugin_api.py"
    assert plugin_file.exists(), f"plugin file missing: {plugin_file}"
    mod_name = "hermes_dashboard_plugin_sligo_test"
    sys.modules.pop(mod_name, None)
    spec = importlib.util.spec_from_file_location(mod_name, plugin_file)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def sligo_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    workdir = tmp_path / "configured-workspace"
    workdir.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg = {
        "self_improvement": {
            "proposals": {
                "projects": {
                    "sligo": {
                        "default_prong": "r1",
                        "worker_prompt": "Implement this approved proposal.",
                        "acceptance_criteria": ["Run focused tests."],
                        "kanban_board": "default",
                        "workspace_kind": "dir",
                        "workspace_path": str(workdir),
                        "assignee": "dev",
                        "skills": ["kanban-worker"],
                        "prongs": {"r1": {"name": "Round 1"}},
                    }
                }
            }
        }
    }
    (home / "config.yaml").write_text(json.dumps(cfg), encoding="utf-8")
    kb.init_db()
    return home


@pytest.fixture
def client(sligo_home):
    app = FastAPI()
    app.include_router(_load_plugin_module().router, prefix="/api/plugins/sligo")
    return TestClient(app)


def _ingest_card(client: TestClient, *, metadata: dict | None = None) -> int:
    run_resp = client.post(
        "/api/plugins/sligo/runs",
        json={"project_key": "sligo", "prong_key": "r1", "idempotency_key": "run-1"},
    )
    assert run_resp.status_code == 200, run_resp.text
    run_id = run_resp.json()["run"]["id"]
    card_resp = client.post(
        "/api/plugins/sligo/proposals",
        json={
            "run_id": run_id,
            "idempotency_key": "card-1",
            "title": "Tighten dashboard API",
            "summary": "Add safe approval endpoints.",
            "body": "Use stored proposal content only.",
            "rationale": "Operators need the explicit parser detail.",
            "evidence_bullets": ["Evidence from cron output."],
            "acceptance_criteria": ["Detail drawer shows evidence."],
            "proposed_worker_prompt": "Use the stored worker prompt.",
            "priority": "high",
            "metadata": metadata or {},
        },
    )
    assert card_resp.status_code == 200, card_resp.text
    return card_resp.json()["proposal"]["id"]


def _ingest_run_with_output(client: TestClient, output_path: Path) -> tuple[int, str]:
    run_resp = client.post(
        "/api/plugins/sligo/runs",
        json={
            "project_key": "sligo",
            "prong_key": "r1",
            "idempotency_key": "run-with-output",
            "raw_input_ref": str(output_path),
        },
    )
    assert run_resp.status_code == 200, run_resp.text
    run = run_resp.json()["run"]
    card_resp = client.post(
        "/api/plugins/sligo/proposals",
        json={
            "run_id": run["id"],
            "idempotency_key": "card-with-output",
            "title": "Tighten dashboard API",
            "summary": "Add safe approval endpoints.",
            "source_metadata": {"source_output_path": str(output_path)},
        },
    )
    assert card_resp.status_code == 200, card_resp.text
    return card_resp.json()["proposal"]["id"], run["source_output_ref"]


def test_read_routes_expose_projects_runs_and_proposals(client):
    proposal_id = _ingest_card(client)

    projects = client.get("/api/plugins/sligo/projects")
    proposals_resp = client.get("/api/plugins/sligo/proposals")
    proposal_resp = client.get(f"/api/plugins/sligo/proposals/{proposal_id}")
    runs_resp = client.get("/api/plugins/sligo/runs")
    run_id = runs_resp.json()["runs"][0]["id"]
    run_resp = client.get(f"/api/plugins/sligo/runs/{run_id}")

    assert projects.status_code == 200
    assert projects.json()["projects"][0]["key"] == "sligo"
    assert proposals_resp.status_code == 200
    assert [p["id"] for p in proposals_resp.json()["proposals"]] == [proposal_id]
    assert proposal_resp.status_code == 200
    detail = proposal_resp.json()["proposal"]
    assert detail["title"] == "Tighten dashboard API"
    assert detail["rationale"] == "Operators need the explicit parser detail."
    assert detail["evidence_bullets"] == ["Evidence from cron output."]
    assert detail["acceptance_criteria"] == ["Detail drawer shows evidence."]
    assert detail["proposed_worker_prompt"] == "Use the stored worker prompt."
    assert detail["audit_log"][0]["action"] == "ingested"
    assert "metadata" not in detail
    assert runs_resp.status_code == 200
    assert run_resp.status_code == 200
    assert run_resp.json()["proposals"][0]["id"] == proposal_id


def test_source_output_ref_opens_saved_cron_markdown(client, sligo_home):
    output_dir = sligo_home / "cron" / "output" / "2026-06-03"
    output_dir.mkdir(parents=True)
    output_path = output_dir / "proposal.md"
    output_path.write_text("# Saved cron proposal\n\nCron output body.\n", encoding="utf-8")

    proposal_id, ref = _ingest_run_with_output(client, output_path)
    proposal_resp = client.get(f"/api/plugins/sligo/proposals/{proposal_id}")
    opened = client.get(f"/api/plugins/sligo/source-output/{ref}")

    assert proposal_resp.status_code == 200, proposal_resp.text
    proposal = proposal_resp.json()["proposal"]
    assert proposal["source_output_ref"] == ref
    assert proposal["source_output_url"] == f"/api/plugins/sligo/source-output/{ref}"
    assert str(output_path) not in proposal["source_output_url"]
    assert opened.status_code == 200, opened.text
    assert opened.headers["content-type"].startswith("text/markdown")
    assert opened.text == "# Saved cron proposal\n\nCron output body.\n"


def test_source_output_uses_authenticated_dashboard_fetch(sligo_home):
    from hermes_cli import web_server

    output_dir = sligo_home / "cron" / "output" / "2026-06-03"
    output_dir.mkdir(parents=True)
    output_path = output_dir / "proposal.md"
    output_path.write_text("# Saved cron proposal\n\nAuthenticated body.\n", encoding="utf-8")
    ref = proposals.safe_source_output_ref(output_path)

    web_client = TestClient(web_server.app)
    raw = web_client.get(f"/api/plugins/sligo/source-output/{ref}")
    authed = web_client.get(
        f"/api/plugins/sligo/source-output/{ref}",
        headers={web_server._SESSION_HEADER_NAME: web_server._SESSION_TOKEN},
    )

    assert raw.status_code == 401
    assert authed.status_code == 200, authed.text
    assert authed.headers["content-type"].startswith("text/markdown")
    assert authed.text == "# Saved cron proposal\n\nAuthenticated body.\n"


def test_sligo_source_output_control_is_not_plain_api_anchor():
    repo_root = Path(__file__).resolve().parents[2]
    bundle = (repo_root / "plugins" / "sligo" / "dashboard" / "dist" / "index.js").read_text(encoding="utf-8")

    assert "authenticatedFetch(outputUrl)" in bundle
    assert 'href: source, target: "_blank"' not in bundle


def test_source_output_route_rejects_unsafe_and_missing_refs(client, sligo_home, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    missing = sligo_home / "cron" / "output" / "missing.md"

    outside_ref = proposals.safe_source_output_ref(outside)
    missing_ref = proposals.safe_source_output_ref(missing)

    outside_resp = client.get("/api/plugins/sligo/source-output/not-a-valid-ref")
    missing_resp = client.get(f"/api/plugins/sligo/source-output/{missing_ref}")

    assert outside_ref == ""
    assert outside_resp.status_code == 400
    assert missing_resp.status_code == 404


def test_approve_twice_is_idempotent_and_uses_configured_workspace(client, sligo_home):
    proposal_id = _ingest_card(client, metadata={"workspace_path": "/attacker/override"})

    first = client.post(
        f"/api/plugins/sligo/proposals/{proposal_id}/approve",
        json={"reason": "Good scope", "feedback": "Keep it small."},
        headers={"X-Hermes-Operator": "operator-1"},
    )
    second = client.post(f"/api/plugins/sligo/proposals/{proposal_id}/approve", json={})

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    first_proposal = first.json()["proposal"]
    second_proposal = second.json()["proposal"]
    assert second_proposal["worker_task_id"] == first_proposal["worker_task_id"]
    assert second_proposal["approved_by"] == "operator-1"

    with kb.connect_closing(board="default") as conn:
        count = conn.execute("SELECT COUNT(*) FROM tasks WHERE idempotency_key = ?", (f"sligo:proposal:{proposal_id}",)).fetchone()[0]
        task = kb.get_task(conn, first_proposal["worker_task_id"])

    assert count == 1
    assert task is not None
    assert task.workspace_path == str(sligo_home.parent / "configured-workspace")
    assert task.workspace_path != "/attacker/override"
    assert task.assignee == "dev"
    assert task.status == "ready"
    assert "Use the stored worker prompt." in task.body
    assert "Evidence from cron output." in task.body
    assert "Detail drawer shows evidence." in task.body


def test_reject_records_decision_and_default_list_excludes_it(client):
    proposal_id = _ingest_card(client)

    reject = client.post(
        f"/api/plugins/sligo/proposals/{proposal_id}/reject",
        json={"reason": "Too broad", "strength": "hard", "feedback": "Split into smaller pieces."},
        headers={"X-Hermes-Operator": "operator-2"},
    )
    default_list = client.get("/api/plugins/sligo/proposals")
    inactive_list = client.get("/api/plugins/sligo/proposals?include_inactive=true")

    assert reject.status_code == 200, reject.text
    rejected = reject.json()["proposal"]
    assert rejected["status"] == "rejected"
    assert rejected["rejected_by"] == "operator-2"
    assert rejected["decision_reason"] == "Too broad [strength=hard]"
    assert rejected["operator_feedback"] == "Split into smaller pieces."
    assert default_list.json()["proposals"] == []
    assert [p["id"] for p in inactive_list.json()["proposals"]] == [proposal_id]


def test_patch_feedback_and_bulk_guard(client):
    proposal_id = _ingest_card(client)

    patched = client.patch(
        f"/api/plugins/sligo/proposals/{proposal_id}",
        json={"title": "Updated title", "tags": ["api"], "status": "approved"},
    )
    feedback = client.post(
        f"/api/plugins/sligo/proposals/{proposal_id}/feedback",
        json={"reason": "Direction", "feedback": "Prefer route-level tests."},
    )
    bulk = client.post("/api/plugins/sligo/proposals/bulk-approve", json={"proposal_ids": [proposal_id]})

    assert patched.status_code == 200, patched.text
    assert patched.json()["proposal"]["title"] == "Updated title"
    assert patched.json()["proposal"]["status"] == "proposed"
    assert patched.json()["proposal"]["tags"] == ["api"]
    assert feedback.status_code == 200, feedback.text
    assert feedback.json()["proposal"]["operator_feedback"] == "Prefer route-level tests."
    assert bulk.status_code == 409


def test_unknown_project_ingest_is_rejected(client):
    resp = client.post(
        "/api/plugins/sligo/runs",
        json={"project_key": "missing", "prong_key": "r1", "idempotency_key": "run-unknown"},
    )

    assert resp.status_code == 404


def test_mutations_are_protected_by_dashboard_auth(sligo_home):
    from hermes_cli import web_server

    web_client = TestClient(web_server.app)
    resp = web_client.post(
        "/api/plugins/sligo/runs",
        json={"project_key": "sligo", "prong_key": "r1", "idempotency_key": "run-auth"},
    )

    authed = web_client.post(
        "/api/plugins/sligo/runs",
        json={"project_key": "sligo", "prong_key": "r1", "idempotency_key": "run-auth"},
        headers={web_server._SESSION_HEADER_NAME: web_server._SESSION_TOKEN},
    )

    assert resp.status_code == 401
    assert authed.status_code == 200, authed.text
