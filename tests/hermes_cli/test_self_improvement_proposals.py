from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from hermes_cli.config import DEFAULT_CONFIG, load_config
from hermes_cli import self_improvement_proposals as sip


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
                    "skills": ["python"],
                    "prongs": {
                        "airflow_doctor": {"name": "Airflow Doctor", "enabled": True},
                        "admin_dogfood": {"name": "Admin Dogfood", "enabled": True, "assignee": "admin-dev"},
                    },
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
                "title": "Add Airflow DAG health summary",
                "summary": "Expose failed DAGs in the admin view.",
                "body": "Operators currently need to inspect logs manually.",
                "evidence": [
                    {"label": "log", "detail": "dag failed", "api_token": "secret-token"}
                ],
                "worker_prompt": "Implement a read-only failed DAG summary.",
                "acceptance_criteria": ["Failed DAGs are visible without credentials."],
                "priority": "high",
                "confidence": 0.75,
                "effort": "small",
                "suggested_assignee": "somebody-else",
                "suggested_skills": ["airflow"],
            }
        ],
    }


def test_default_config_contains_sligo_project_and_required_prongs(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    cfg = load_config()
    si = cfg["self_improvement"]

    assert si["enabled"] is False
    assert list(si["projects"])[0] == "sligo"
    assert set(si["projects"]["sligo"]["prongs"]) == {
        "airflow_doctor",
        "admin_dogfood",
        "invisible_technical_recommendations",
        "visible_ui_ux_recommendations",
    }
    assert DEFAULT_CONFIG["self_improvement"]["storage_db"] == "self_improvement/proposals.db"


def test_connect_initializes_profile_scoped_db_and_tables(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    sip._INITIALIZED_PATHS.clear()

    with sip.connect(config=load_config()) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    assert sip.proposal_db_path(load_config()) == home / "self_improvement" / "proposals.db"
    assert {"proposal_runs", "proposal_cards", "proposal_feedback"}.issubset(tables)
    assert not (home / "kanban.db").exists()


def test_valid_ingestion_from_metadata_stores_run_card_source_and_sanitizes(tmp_path):
    cfg = _config(tmp_path)
    source = tmp_path / "cron-output.txt"
    source.write_text("cron output", encoding="utf-8")
    result = sip.ingest_proposal_output(
        metadata={
            "proposal_json": json.dumps(_payload()),
            "cron_output_path": str(source),
            "cron_job_id": "job-1",
            "cron_job_name": "Sligo proposals",
            "cron_run_id": "run-1",
            "model": "test-model",
            "provider": "test-provider",
            "profile": "dev",
            "workdir": str(tmp_path),
        },
        config=cfg,
    )

    assert result["parse_status"] == "parsed"
    assert len(result["card_ids"]) == 1

    runs = sip.list_runs(project="sligo", config=cfg)
    assert runs[0]["cron_output_path"] == str(source)
    assert len(runs[0]["cron_output_sha256"]) == 64
    assert "metadata_json" not in runs[0]

    cards = sip.list_proposals(project="sligo", prong="airflow_doctor", config=cfg)
    assert cards[0]["title"] == "Add Airflow DAG health summary"
    assert cards[0]["resolved_workspace_path"] == str(tmp_path / "sligo")
    assert cards[0]["resolved_board"] == "sligo-board"
    assert cards[0]["resolved_assignee"] == "dev"
    assert cards[0]["resolved_skills"] == ["python"]
    assert cards[0]["suggested_assignee"] == "somebody-else"
    assert cards[0]["evidence"][0]["api_token"] == "[REDACTED]"

    with sqlite3.connect(str(tmp_path / "proposals.db")) as conn:
        stored = conn.execute("SELECT evidence_json FROM proposal_cards").fetchone()[0]
    assert "secret-token" not in stored


def test_ingestion_from_marked_output_text(tmp_path):
    cfg = _config(tmp_path)
    text = (
        "before\n"
        f"{sip.START_MARKER}\n"
        f"{json.dumps(_payload())}\n"
        f"{sip.END_MARKER}\n"
        "after"
    )

    result = sip.ingest_proposal_output(metadata={"run_id": "marked-run"}, output_text=text, config=cfg)

    assert result["parse_status"] == "parsed"
    assert sip.list_runs(config=cfg)[0]["cron_output_sha256"]


def test_malformed_json_creates_visible_parse_failure_run(tmp_path):
    cfg = _config(tmp_path)

    result = sip.ingest_proposal_output(
        metadata={"proposal_json": "{not json", "cron_run_id": "bad-run"},
        config=cfg,
    )

    assert result["parse_status"] == "parse_error"
    assert result["card_ids"] == []
    runs = sip.list_runs(parse_status="parse_error", config=cfg)
    assert runs[0]["parse_status"] == "parse_error"
    assert "Malformed proposal JSON" in runs[0]["parse_error"]
    assert sip.list_proposals(config=cfg) == []


def test_strict_contract_rejects_unexpected_workspace_field(tmp_path):
    cfg = _config(tmp_path)
    payload = _payload()
    payload["proposals"][0]["workspace_path"] = "/tmp/evil"

    result = sip.ingest_proposal_output(
        metadata={"proposal_json": json.dumps(payload), "cron_run_id": "bad-workspace"},
        config=cfg,
    )

    assert result["parse_status"] == "parse_error"
    assert "Unexpected proposal fields: workspace_path" in result["parse_error"]


def test_listing_filters_and_detail(tmp_path):
    cfg = _config(tmp_path)
    payload = _payload()
    sip.ingest_proposal_output(metadata={"proposal_json": json.dumps(payload), "cron_run_id": "run-a"}, config=cfg)
    payload["prong"] = "admin_dogfood"
    payload["proposals"][0]["title"] = "Improve admin dogfood"
    payload["proposals"][0]["summary"] = "Use admin flows daily."
    sip.ingest_proposal_output(metadata={"proposal_json": json.dumps(payload), "cron_run_id": "run-b"}, config=cfg)

    cards = sip.list_proposals(prong="admin_dogfood", config=cfg)

    assert len(cards) == 1
    assert cards[0]["resolved_assignee"] == "admin-dev"
    assert sip.get_proposal_detail(cards[0]["card_id"], config=cfg)["title"] == "Improve admin dogfood"
    assert len(sip.list_runs(project="sligo", config=cfg)) == 2


def test_project_validation_helpers_resolve_only_trusted_config(tmp_path):
    cfg = _config(tmp_path)
    workspace = tmp_path / "sligo"
    workspace.mkdir()

    context = sip.resolve_execution_context("sligo", "airflow_doctor", cfg)

    assert context["workspace_path"] == str(workspace)
    assert sip.validate_approval_project_workspace("sligo", str(workspace), cfg) is True
    with pytest.raises(ValueError, match="Unknown self_improvement prong"):
        sip.resolve_execution_context("sligo", "missing", cfg)
    with pytest.raises(ValueError, match="Workspace does not match"):
        sip.validate_approval_project_workspace("sligo", str(tmp_path), cfg)
