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


def test_default_config_contains_pid_project_and_required_prongs(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    cfg = load_config()
    si = cfg["self_improvement"]

    assert si["enabled"] is False
    assert list(si["projects"])[0] == "pid"
    assert si["projects"]["pid"]["name"] == "PID"
    assert set(si["projects"]["pid"]["prongs"]) == {
        "airflow_doctor",
        "admin_dogfood",
        "invisible_technical_recommendations",
        "visible_ui_ux_recommendations",
    }
    assert si["projects"]["pid"]["prongs"]["airflow_doctor"]["cron_job_names"] == [
        "Nightly PID Airflow scraper doctor"
    ]
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
    source = tmp_path / "cron-output-sk-proj-sourceSecret1234567890.txt"
    source.write_text("cron output", encoding="utf-8")
    payload = _payload()
    payload["proposals"][0].update(
        {
            "title": "Add Airflow summary sk-proj-titleSecret1234567890",
            "summary": "Expose failed DAGs using sk-proj-summarySecret1234567890 in neutral prose.",
            "body": "Operators saw OPENAI_API_KEY=bodySecret1234567890 during triage.",
            "worker_prompt": "Implement without leaking ghp_workerSecret1234567890.",
            "evidence": [
                {
                    "label": "log sk-proj-labelSecret1234567890",
                    "detail": "dag failed with token ghp_detailSecret1234567890",
                    "api_token": "secret-token",
                }
            ],
        }
    )
    result = sip.ingest_proposal_output(
        metadata={
            "proposal_json": json.dumps(payload),
            "cron_output_path": str(source),
            "cron_job_id": "job-1",
            "cron_job_name": "Sligo proposals sk-proj-jobSecret1234567890",
            "cron_run_id": "run-1",
            "model": "test-model sk-proj-modelSecret1234567890",
            "provider": "test-provider",
            "profile": "dev",
            "workdir": f"{tmp_path}/sk-proj-workdirSecret1234567890",
        },
        config=cfg,
    )

    assert result["parse_status"] == "parsed"
    assert len(result["card_ids"]) == 1

    runs = sip.list_runs(project="sligo", config=cfg)
    assert "sk-proj-sourceSecret1234567890" not in runs[0]["cron_output_path"]
    assert "sk-proj-jobSecret1234567890" not in runs[0]["cron_job_name"]
    assert "sk-proj-modelSecret1234567890" not in runs[0]["model"]
    assert "sk-proj-workdirSecret1234567890" not in runs[0]["workdir"]
    assert len(runs[0]["cron_output_sha256"]) == 64
    assert "metadata_json" not in runs[0]

    cards = sip.list_proposals(project="sligo", prong="airflow_doctor", config=cfg)
    assert "sk-proj-titleSecret1234567890" not in cards[0]["title"]
    assert "sk-proj-summarySecret1234567890" not in cards[0]["summary"]
    assert "bodySecret1234567890" not in cards[0]["body"]
    assert "ghp_workerSecret1234567890" not in cards[0]["worker_prompt"]
    assert cards[0]["resolved_workspace_path"] == str(tmp_path / "sligo")
    assert cards[0]["resolved_board"] == "sligo-board"
    assert cards[0]["resolved_assignee"] == "dev"
    assert cards[0]["resolved_skills"] == ["python"]
    assert cards[0]["suggested_assignee"] == "somebody-else"
    assert cards[0]["evidence"][0]["api_token"] == "[REDACTED]"
    assert "sk-proj-labelSecret1234567890" not in cards[0]["evidence"][0]["label"]
    assert "ghp_detailSecret1234567890" not in cards[0]["evidence"][0]["detail"]
    detail = sip.get_proposal_detail(cards[0]["card_id"], config=cfg)
    assert "ghp_workerSecret1234567890" not in detail["worker_prompt"]

    with sqlite3.connect(str(tmp_path / "proposals.db")) as conn:
        stored_row = conn.execute(
            "SELECT title, summary, body, worker_prompt, evidence_json, source_output_path FROM proposal_cards"
        ).fetchone()
        stored_run = conn.execute("SELECT cron_job_name, model, workdir, metadata_json FROM proposal_runs").fetchone()
    stored = " ".join(str(value) for value in (*stored_row, *stored_run))
    assert "secret-token" not in stored
    assert "sk-proj-titleSecret1234567890" not in stored
    assert "sk-proj-summarySecret1234567890" not in stored
    assert "bodySecret1234567890" not in stored
    assert "ghp_workerSecret1234567890" not in stored
    assert "sk-proj-labelSecret1234567890" not in stored
    assert "ghp_detailSecret1234567890" not in stored
    assert "sk-proj-sourceSecret1234567890" not in stored
    assert "sk-proj-jobSecret1234567890" not in stored
    assert "sk-proj-modelSecret1234567890" not in stored
    assert "sk-proj-workdirSecret1234567890" not in stored


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


def test_feedback_context_summarizes_approved_rejected_preferences_and_edges(tmp_path):
    cfg = _config(tmp_path)
    sip.ingest_proposal_output(metadata={"proposal_json": json.dumps(_payload()), "cron_run_id": "run-a"}, config=cfg)
    approved_id = sip.list_proposals(config=cfg)[0]["card_id"]
    sip.approve_proposal(approved_id, actor="operator", config=cfg)
    sip.add_feedback(approved_id, feedback_type="comment", body="Prefer read-only observability with tiny worker prompts.", author="operator", config=cfg)

    payload = _payload()
    payload["proposals"][0]["title"] = "Rewrite the admin shell"
    payload["proposals"][0]["summary"] = "Large shell replacement."
    payload["proposals"][0]["worker_prompt"] = "Replace all admin shell code."
    sip.ingest_proposal_output(metadata={"proposal_json": json.dumps(payload), "cron_run_id": "run-b"}, config=cfg)
    rejected_id = [p for p in sip.list_proposals(config=cfg) if p["card_id"] != approved_id][0]["card_id"]
    sip.reject_proposal(rejected_id, reason="Too broad; split into smaller read-only slices.", actor="operator", config=cfg)

    context = sip.build_feedback_context("sligo", "airflow_doctor", config=cfg)

    assert context["project"] == "sligo"
    assert context["recent_approved_proposals"][0]["title"] == "Add Airflow DAG health summary"
    assert context["recent_rejected_proposals"][0]["reason"] == "Too broad; split into smaller read-only slices."
    preference_bodies = [item["body"] for item in context["operator_preferences_and_patterns"]]
    assert "Prefer read-only observability with tiny worker prompts." in preference_bodies


def test_approve_is_idempotent_and_records_one_audit_event(tmp_path):
    cfg = _config(tmp_path)
    sip.ingest_proposal_output(metadata={"proposal_json": json.dumps(_payload()), "cron_run_id": "run-a"}, config=cfg)
    card_id = sip.list_proposals(config=cfg)[0]["card_id"]

    first = sip.approve_proposal(card_id, actor="operator", config=cfg)
    second = sip.approve_proposal(card_id, actor="operator", config=cfg)

    assert second["linked_kanban_task_id"] == first["linked_kanban_task_id"]
    assert second["linked_kanban_board"] == "sligo-board"
    assert second["linked_worker_url"]
    assert second["worker"]["url"] == first["worker"]["url"]
    feedback = sip.list_feedback(card_id, config=cfg)
    assert [item["feedback_type"] for item in feedback] == ["approve"]
    with sip.kanban_db.connect(board="sligo-board") as conn:
        rows = conn.execute("SELECT * FROM tasks WHERE idempotency_key = ?", (f"self-improvement:{card_id}",)).fetchall()
    assert len(rows) == 1


def test_approve_after_linked_task_archived_returns_existing_link_without_new_task(tmp_path):
    cfg = _config(tmp_path)
    sip.ingest_proposal_output(metadata={"proposal_json": json.dumps(_payload()), "cron_run_id": "run-a"}, config=cfg)
    card_id = sip.list_proposals(config=cfg)[0]["card_id"]
    approved = sip.approve_proposal(card_id, actor="operator", config=cfg)
    task_id = approved["linked_kanban_task_id"]

    with sip.kanban_db.connect(board="sligo-board") as conn:
        conn.execute("UPDATE tasks SET status = 'archived' WHERE id = ?", (task_id,))

    repeated = sip.approve_proposal(card_id, actor="operator", config=cfg)

    assert repeated["linked_kanban_task_id"] == task_id
    assert repeated["worker"]["task_id"] == task_id
    feedback = sip.list_feedback(card_id, config=cfg)
    assert [item["feedback_type"] for item in feedback] == ["approve"]
    with sip.kanban_db.connect(board="sligo-board") as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE idempotency_key = ?", (f"self-improvement:{card_id}",)).fetchone()[0] == 1


def test_repeated_reject_is_idempotent_and_records_one_audit_event(tmp_path):
    cfg = _config(tmp_path)
    sip.ingest_proposal_output(metadata={"proposal_json": json.dumps(_payload()), "cron_run_id": "run-a"}, config=cfg)
    card_id = sip.list_proposals(config=cfg)[0]["card_id"]

    first = sip.reject_proposal(card_id, reason="Too broad", strength="strong", actor="operator", config=cfg)
    second = sip.reject_proposal(card_id, reason="Different reason", strength="weak", actor="other", config=cfg)

    assert second["status"] == "rejected"
    assert second["decision_reason"] == first["decision_reason"] == "Too broad"
    feedback = sip.list_feedback(card_id, config=cfg)
    assert len(feedback) == 1
    assert feedback[0]["feedback_type"] == "reject"
    assert feedback[0]["body"] == "Too broad"


def test_feedback_context_handles_missing_feedback_and_malformed_metadata(tmp_path):
    cfg = _config(tmp_path)
    sip.ingest_proposal_output(metadata={"proposal_json": json.dumps(_payload()), "cron_run_id": "run-a"}, config=cfg)
    card_id = sip.list_proposals(config=cfg)[0]["card_id"]
    with sqlite3.connect(str(tmp_path / "proposals.db")) as conn:
        conn.execute(
            "INSERT INTO proposal_feedback (card_id, feedback_type, body, author, metadata_json, created_at) VALUES (?, 'comment', '', 'op', '{bad', 1)",
            (card_id,),
        )

    context = sip.build_feedback_context("sligo", "airflow_doctor", config=cfg)

    assert context["recent_approved_proposals"] == []
    assert context["recent_rejected_proposals"] == []
    assert context["operator_preferences_and_patterns"] == []


def test_correlate_linked_kanban_status_updates_lifecycle_and_feedback(tmp_path):
    cfg = _config(tmp_path)
    sip.ingest_proposal_output(metadata={"proposal_json": json.dumps(_payload()), "cron_run_id": "run-a"}, config=cfg)
    card_id = sip.list_proposals(config=cfg)[0]["card_id"]
    approved = sip.approve_proposal(card_id, actor="operator", config=cfg)

    from hermes_cli import kanban_db

    board = approved["linked_kanban_board"]
    task_id = approved["linked_kanban_task_id"]
    with kanban_db.connect(board=board) as conn:
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (task_id,))

    result = sip.correlate_linked_kanban_outcomes(project="sligo", prong="airflow_doctor", config=cfg)

    detail = sip.get_proposal_detail(card_id, config=cfg)
    feedback = sip.list_feedback(card_id, config=cfg)
    assert result["updated"] == 1
    assert detail["lifecycle_status"] == "completed"
    assert any(item["feedback_type"] == "kanban_status" and " is done" in item["body"] for item in feedback)


def test_build_proposal_prompt_context_contains_contract_and_feedback(tmp_path):
    cfg = _config(tmp_path)
    sip.ingest_proposal_output(metadata={"proposal_json": json.dumps(_payload()), "cron_run_id": "run-a"}, config=cfg)
    card_id = sip.list_proposals(config=cfg)[0]["card_id"]
    sip.reject_proposal(card_id, reason="Needs stronger evidence.", config=cfg)

    block = sip.build_proposal_prompt_context("sligo", "airflow_doctor", config=cfg)

    assert sip.PROMPT_CONTEXT_MARKER in block
    assert sip.START_MARKER in block
    assert sip.END_MARKER in block
    assert '"hermes_self_improvement_proposals_version": 1' in block
    assert '"project": "sligo"' in block
    assert '"prong": "airflow_doctor"' in block
    assert "Needs stronger evidence." in block


def test_cron_job_prompt_context_uses_job_opt_in_and_config_names(tmp_path):
    cfg = _config(tmp_path)
    cfg["self_improvement"]["projects"]["sligo"]["prongs"]["airflow_doctor"].update(
        {"cron_prompt_context": True, "cron_job_names": ["Sligo Airflow Doctor"]}
    )

    by_job = sip.build_cron_job_prompt_context(
        {"self_improvement": {"project": "sligo", "prong": "airflow_doctor"}},
        config=cfg,
    )
    by_name = sip.build_cron_job_prompt_context({"name": "Sligo Airflow Doctor"}, config=cfg)

    assert sip.PROMPT_CONTEXT_MARKER in by_job
    assert sip.PROMPT_CONTEXT_MARKER in by_name
    assert sip.build_cron_job_prompt_context({"name": "Unrelated"}, config=cfg) == ""
