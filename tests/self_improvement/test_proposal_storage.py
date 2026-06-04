import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from self_improvement import proposal_storage

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "self_improvement"


def _fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _fenced(name: str) -> str:
    return "Human summary before JSON.\n```json\n" + _fixture_text(name) + "\n```"


def test_profile_safe_db_path_and_initialization(tmp_path, monkeypatch):
    home = tmp_path / "profile-home"
    monkeypatch.setenv("HERMES_HOME", str(home))

    proposal_storage.init_db()

    assert proposal_storage.proposals_db_path() == home / "self_improvement" / "proposals.db"
    assert proposal_storage.proposals_db_path().exists()


def test_ingest_valid_run_is_idempotent_and_replaces_cards(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))

    first = proposal_storage.ingest_proposal_output(_fenced("proposal_run_pid_valid.json"))
    second = proposal_storage.ingest_proposal_output(_fenced("proposal_run_pid_valid.json"))

    assert first["status"] == "valid"
    assert first["run_id"] == second["run_id"]
    assert second["card_count"] == 1

    conn = proposal_storage.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM proposal_runs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM proposal_cards").fetchone()[0] == 1
    finally:
        conn.close()


def test_ingest_empty_run_records_empty_status(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))

    result = proposal_storage.ingest_proposal_output(_fenced("proposal_run_pid_empty.json"))

    run = proposal_storage.get_run(result["run_id"])

    assert result["status"] == "empty"
    assert run["status"] == "empty"
    assert run["cards"] == []


def test_ingest_malformed_run_persists_parse_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))

    result = proposal_storage.ingest_proposal_output(
        _fenced("proposal_run_malformed.json"),
        source={"cron_job_id": "job-1", "run_id": "run-1", "cron_output_path": "/tmp/out.md"},
    )
    failures = proposal_storage.list_parse_failures()

    assert result["status"] == "malformed"
    assert "run.cron_job_id" in result["parse_error"]
    assert len(failures["failures"]) == 1
    assert failures["failures"][0]["source_ref"]["cron_output_path"] == "/tmp/out.md"


def test_grouped_reads_card_details_and_run_source(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    result = proposal_storage.ingest_proposal_output(_fenced("proposal_run_pid_valid.json"))

    grouped = proposal_storage.grouped_cards()
    prong = grouped["projects"][0]["prongs"][0]
    card = prong["cards"][0]
    detail = proposal_storage.get_card(card["proposal_id"])
    run = proposal_storage.get_run(result["run_id"])

    assert grouped["projects"][0]["project"] == "pid"
    assert prong["prong"] == "airflow_scraper_doctor"
    assert detail["kanban_task"]["title"] == "Instrument PID scraper timeout retry backoff"
    assert run["source_markdown"].startswith("Human summary before JSON")
    assert run["cards"][0]["proposal_id"] == card["proposal_id"]


def test_dashboard_read_only_routes_return_proposal_shapes(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    proposal_storage.ingest_proposal_output(_fenced("proposal_run_pid_valid.json"))
    proposal_storage.ingest_proposal_output(
        "not json",
        source={"source_key": "bad-output", "cron_output_path": "/tmp/bad.md"},
    )

    from plugins.kanban.dashboard.plugin_api import router

    app = FastAPI()
    app.include_router(router, prefix="/api/plugins/kanban")
    client = TestClient(app)

    grouped = client.get("/api/plugins/kanban/self-improvement/proposals")
    assert grouped.status_code == 200
    card = grouped.json()["projects"][0]["prongs"][0]["cards"][0]

    detail = client.get(f"/api/plugins/kanban/self-improvement/proposals/{card['proposal_id']}")
    assert detail.status_code == 200
    assert detail.json()["card"]["proposal_id"] == card["proposal_id"]

    run = client.get(f"/api/plugins/kanban/self-improvement/runs/{card['run_db_id']}")
    assert run.status_code == 200
    assert run.json()["run"]["source_markdown"]

    failures = client.get("/api/plugins/kanban/self-improvement/parse-failures")
    assert failures.status_code == 200
    assert failures.json()["failures"][0]["source_ref"]["cron_output_path"] == "/tmp/bad.md"


def test_json_fence_parser_accepts_plain_json(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    payload = json.loads(_fixture_text("proposal_run_pid_empty.json"))

    result = proposal_storage.ingest_proposal_output(json.dumps(payload))

    assert result["status"] == "empty"
