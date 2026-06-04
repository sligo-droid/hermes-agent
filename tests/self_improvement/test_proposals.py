import json
from pathlib import Path

import pytest

from hermes_cli.config import DEFAULT_CONFIG, load_config
from self_improvement.proposals import (
    CONTRACT_VERSION,
    ProposalValidationError,
    build_cron_proposal_guidance,
    validate_proposal_run,
)


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "self_improvement"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_default_config_contains_pid_self_improvement_prongs():
    cfg = load_config()
    section = cfg["self_improvement"]
    pid = section["projects"]["pid"]

    assert section["proposal_contract_version"] == CONTRACT_VERSION
    assert section["default_max_cards_per_run"] == 5
    assert set(pid["prongs"]) >= {
        "airflow_scraper_doctor",
        "admin_dogfood_ux_bugfix",
        "invisible_technical_recommendations",
        "visible_ui_ux_recommendations",
    }
    assert DEFAULT_CONFIG["self_improvement"]["projects"]["pid"]["prongs"]["airflow_scraper_doctor"]["max_cards_per_run"] == 5


def test_valid_pid_proposal_run_is_accepted_and_gets_stable_id():
    payload = _fixture("proposal_run_pid_valid.json")

    normalized = validate_proposal_run(payload)

    assert normalized["contract_version"] == CONTRACT_VERSION
    assert normalized["project"] == "pid"
    assert normalized["prong"] == "airflow_scraper_doctor"
    assert normalized["cards"][0]["proposal_id"].startswith("pid-airflow_scraper_doctor-")
    assert validate_proposal_run(payload)["cards"][0]["proposal_id"] == normalized["cards"][0]["proposal_id"]


def test_empty_pid_proposal_run_is_accepted():
    normalized = validate_proposal_run(_fixture("proposal_run_pid_empty.json"))

    assert normalized["cards"] == []
    assert normalized["human_markdown"].startswith("No new")


def test_malformed_proposal_run_is_rejected():
    with pytest.raises(ProposalValidationError) as excinfo:
        validate_proposal_run(_fixture("proposal_run_malformed.json"))

    assert "run.cron_job_id" in str(excinfo.value)


def test_unknown_prong_is_rejected():
    payload = _fixture("proposal_run_pid_empty.json")
    payload["prong"] = "made_up_prong"

    with pytest.raises(ProposalValidationError) as excinfo:
        validate_proposal_run(payload)

    assert "unknown self-improvement prong" in str(excinfo.value)


def test_card_cap_is_enforced():
    payload = _fixture("proposal_run_pid_valid.json")
    payload["cards"] = payload["cards"] * 6


    with pytest.raises(ProposalValidationError) as excinfo:
        validate_proposal_run(payload)

    assert "at most 5" in str(excinfo.value)


def test_cron_proposal_guidance_requests_json_and_no_kanban_mutation():
    guidance = build_cron_proposal_guidance("pid", "admin_dogfood_ux_bugfix")

    assert CONTRACT_VERSION in guidance
    assert "```json" in guidance
    assert "at most 5 proposal cards" in guidance
    assert "Do not create Kanban tasks" in guidance
    assert "human markdown" in guidance
    assert "`run_id`" in guidance
    assert "`cron_job_id`" in guidance
    assert "`created_at`" in guidance
    assert "critical`, `high`, `medium`, or `low`" in guidance
    assert "do not use P0/P1/P2" in guidance
    assert "`critical`, `major`, `minor`, or `info`" in guidance
    assert "deterministic string `idempotency_key`" in guidance
    assert "source_excerpts` as objects with a `text` field" in guidance
    assert "Do not put audit notes" in guidance
