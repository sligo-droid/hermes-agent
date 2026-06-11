import json
from pathlib import Path
from unittest.mock import patch

from cron.scheduler import _build_job_prompt, tick
from hermes_cli.config import DEFAULT_CONFIG
from self_improvement import proposal_storage
from self_improvement.proposals import CONTRACT_VERSION


def test_cron_job_prompt_includes_self_improvement_proposal_guidance():
    prompt = _build_job_prompt(
        {
            "id": "abc123def456",
            "name": "Daily PID admin dogfood UX bugfix",
            "prompt": "Review yesterday's PID admin dogfood notes.",
            "self_improvement_proposal": {
                "project": "pid",
                "prong": "admin_dogfood_ux_bugfix",
            },
        }
    )

    assert CONTRACT_VERSION in prompt
    assert "```json" in prompt
    assert "at most 5 proposal cards" in prompt
    assert "only active Command Center Inbox items as duplicate sources" in prompt
    assert "Completed/shipped/done and Archive/rejected/archived Command Center sections" in prompt
    assert "must not suppress a new proposal as an active duplicate" in prompt
    assert "Do not create Kanban tasks" in prompt
    assert "`evidence_basis`" in prompt
    assert "`source_static_log`, `live_browser`, or `blocked_missing_live`" in prompt
    assert "INSUFFICIENT_EVIDENCE" in prompt
    assert "must not imply authenticated live dogfood occurred" in prompt
    assert "Review yesterday's PID admin dogfood notes." in prompt


def test_hermes_self_improvement_prongs_are_valid_for_cron_prompts():
    hermes = DEFAULT_CONFIG["self_improvement"]["projects"]["hermes"]

    assert hermes["label"] == "Hermes"
    assert set(hermes["prongs"]) >= {"daily-retrospective", "system-doctor"}

    for prong in ("daily-retrospective", "system-doctor"):
        prompt = _build_job_prompt(
            {
                "id": f"hermes-{prong}",
                "name": f"Hermes {prong}",
                "prompt": "Review Hermes #dev operations.",
                "self_improvement_proposal": {
                    "project": "hermes",
                    "prong": prong,
                },
            }
        )
        assert "Project: `hermes`" in prompt
        assert f"Prong: `{prong}`" in prompt
        assert CONTRACT_VERSION in prompt
        assert '"project": "hermes"' in prompt
        assert f'"prong": "{prong}"' in prompt


def test_cron_job_prompt_includes_scoped_feedback_context(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    payload = {
        "contract_version": CONTRACT_VERSION,
        "project": "pid",
        "prong": "airflow_scraper_doctor",
        "run": {
            "run_id": "cron-run-1",
            "cron_job_id": "job-1",
            "created_at": "2026-06-04T02:00:00Z",
        },
        "generated_at": "2026-06-04T02:04:10Z",
        "human_markdown": "Two cards.",
        "cards": [
            {
                "idempotency_key": "accept-me",
                "title": "Accepted feedback card",
                "summary": "Approved idea to preserve.",
                "body": "Full accepted body should not be injected.",
                "rationale": "Useful.",
                "priority": "high",
                "status": "proposed",
                "created_at": "2026-06-04T02:04:10Z",
                "source_excerpts": [],
                "kanban_task": {"title": "Accepted task", "body": "Do it."},
            },
            {
                "idempotency_key": "reject-me",
                "title": "Rejected feedback card",
                "summary": "Rejected idea to avoid.",
                "body": "Full rejected body should not be injected.",
                "rationale": "Not useful.",
                "priority": "medium",
                "status": "proposed",
                "created_at": "2026-06-04T02:04:11Z",
                "source_excerpts": [],
                "kanban_task": {"title": "Rejected task", "body": "Do not do it."},
            },
        ],
    }
    import json

    proposal_storage.ingest_proposal_output(json.dumps(payload))
    cards = proposal_storage.grouped_cards()["projects"][0]["prongs"][0]["cards"]
    for card in cards:
        if card["title"].startswith("Accepted"):
            proposal_storage.record_approval(card["proposal_id"], kanban_task_id="t_accept", worker_url="/workers?task=t_accept")
        else:
            proposal_storage.record_rejection(card["proposal_id"], reason="not enough signal")

    prompt = _build_job_prompt(
        {
            "prompt": "Review PID scraper notes.",
            "self_improvement_proposal": {
                "project": "pid",
                "prong": "airflow_scraper_doctor",
            },
        }
    )

    assert "Recent Proposal Feedback" in prompt
    assert "Accepted feedback card" in prompt
    assert "t_accept" in prompt
    assert "Rejected feedback card" in prompt
    assert "not enough signal" in prompt
    assert "Full accepted body should not be injected" not in prompt


def _proposal_payload(*, cards=None, run_id="cron-run-1"):
    return {
        "contract_version": CONTRACT_VERSION,
        "project": "pid",
        "prong": "airflow_scraper_doctor",
        "run": {
            "run_id": run_id,
            "cron_job_id": "proposal-job",
            "cron_job_name": "Proposal cron",
            "created_at": "2026-06-04T02:00:00Z",
        },
        "generated_at": "2026-06-04T02:04:10Z",
        "human_markdown": "Proposal cron summary.",
        "cards": cards if cards is not None else [
            {
                "idempotency_key": "valid-card",
                "title": "Tighten proposal ingestion",
                "summary": "Persist cron proposal output automatically.",
                "body": "Wire cron output into proposal storage after each configured run.",
                "rationale": "Operators need real cron output to populate the board.",
                "priority": "high",
                "severity": "major",
                "status": "proposed",
                "created_at": "2026-06-04T02:04:10Z",
                "source_excerpts": [{"text": "Cron discovered an ingestion gap."}],
                "kanban_task": {"title": "Wire cron proposal ingestion", "body": "Persist configured cron outputs."},
            }
        ],
    }


def _proposal_job(**overrides):
    job = {
        "id": "proposal-job",
        "name": "Proposal cron",
        "prompt": "Generate proposal cards.",
        "schedule": "every 1h",
        "enabled": True,
        "next_run_at": "2020-01-01T00:00:00",
        "deliver": "local",
        "self_improvement_proposal": {
            "project": "pid",
            "prong": "airflow_scraper_doctor",
        },
    }
    job.update(overrides)
    return job


def _run_tick_with_response(tmp_path, monkeypatch, job, final_response):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    output_file = Path(tmp_path) / "cron" / "output" / job["id"] / "2026-06-04_02-04-10.md"
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_doc = f"# Cron Job: {job['name']}\n\n## Response\n\n{final_response}"
    with patch("cron.scheduler._hermes_home", tmp_path / "home"), \
         patch("cron.scheduler.get_due_jobs", return_value=[job]), \
         patch("cron.scheduler.advance_next_run"), \
         patch("cron.scheduler.mark_job_run"), \
         patch("cron.scheduler.save_job_output", side_effect=lambda _job_id, output: output_file), \
         patch("cron.scheduler.run_job", return_value=(True, output_doc, final_response, None)):
        assert tick(verbose=False) == 1
    return output_file


def test_tick_ingests_valid_self_improvement_cron_output(tmp_path, monkeypatch):
    job = _proposal_job(source_url="https://example.invalid/source")
    output_file = _run_tick_with_response(tmp_path, monkeypatch, job, json.dumps(_proposal_payload()))

    grouped = proposal_storage.grouped_cards()
    card = grouped["projects"][0]["prongs"][0]["cards"][0]
    assert card["title"] == "Tighten proposal ingestion"
    run = proposal_storage.get_run(card["run_db_id"])
    assert run["status"] == "valid"
    assert run["cron_job_id"] == "proposal-job"
    assert run["cron_job_name"] == "Proposal cron"
    assert run["cron_output_path"] == str(output_file)
    assert run["source_url"] == "https://example.invalid/source"
    assert run["source_ref"]["source_key"].startswith("cron:proposal-job:")


def test_tick_records_empty_self_improvement_cron_output(tmp_path, monkeypatch):
    _run_tick_with_response(tmp_path, monkeypatch, _proposal_job(), json.dumps(_proposal_payload(cards=[])))

    assert proposal_storage.grouped_cards()["projects"] == []
    runs = proposal_storage.list_runs()["runs"]
    assert runs[0]["status"] == "empty"
    assert runs[0]["card_count"] == 0


def test_tick_records_malformed_self_improvement_cron_output(tmp_path, monkeypatch):
    _run_tick_with_response(tmp_path, monkeypatch, _proposal_job(), "not valid proposal json")

    failures = proposal_storage.list_parse_failures()["failures"]
    assert len(failures) == 1
    assert failures[0]["status"] == "malformed"
    assert "proposal JSON parse error" in failures[0]["parse_error"]


def test_tick_does_not_ingest_silent_self_improvement_cron_output(tmp_path, monkeypatch):
    from cron.scheduler import SILENT_MARKER

    _run_tick_with_response(tmp_path, monkeypatch, _proposal_job(), SILENT_MARKER)

    assert proposal_storage.list_runs()["runs"] == []
    assert proposal_storage.list_parse_failures()["failures"] == []


def test_tick_does_not_ingest_empty_self_improvement_cron_output(tmp_path, monkeypatch):
    _run_tick_with_response(tmp_path, monkeypatch, _proposal_job(), "")

    assert proposal_storage.list_runs()["runs"] == []
    assert proposal_storage.list_parse_failures()["failures"] == []


def test_tick_reingests_same_self_improvement_output_idempotently(tmp_path, monkeypatch):
    job = _proposal_job()
    response = json.dumps(_proposal_payload())
    _run_tick_with_response(tmp_path, monkeypatch, job, response)
    _run_tick_with_response(tmp_path, monkeypatch, job, response)

    grouped = proposal_storage.grouped_cards()
    assert len(grouped["projects"][0]["prongs"][0]["cards"]) == 1
    assert len(proposal_storage.list_runs()["runs"]) == 1


def test_tick_does_not_ingest_non_self_improvement_cron_output(tmp_path, monkeypatch):
    job = _proposal_job()
    job.pop("self_improvement_proposal")
    _run_tick_with_response(tmp_path, monkeypatch, job, json.dumps(_proposal_payload()))

    assert proposal_storage.grouped_cards()["projects"] == []
    assert proposal_storage.list_runs()["runs"] == []
