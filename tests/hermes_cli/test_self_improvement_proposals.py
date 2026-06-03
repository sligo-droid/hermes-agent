import sqlite3
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_cli.config import DEFAULT_CONFIG
from hermes_cli import self_improvement_proposals as proposals


def test_default_config_includes_self_improvement_proposal_defaults():
    cfg = DEFAULT_CONFIG["self_improvement"]["proposals"]

    assert cfg["projects"] == {}
    assert cfg["project_aliases"] == {}
    assert cfg["cron_jobs"] == {}
    assert cfg["feedback_context_limit"] == 20


def test_db_path_uses_hermes_home_and_migrates_without_real_home_writes(tmp_path):
    hermes_home = tmp_path / "hermes-home"
    fake_home = tmp_path / "real-home"
    fake_home.mkdir()

    with patch.dict("os.environ", {"HERMES_HOME": str(hermes_home)}), patch.object(Path, "home", return_value=fake_home):
        db_path = proposals.get_db_path()
        with proposals.connect() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            run_table = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'proposal_runs'"
            ).fetchone()
            card_table = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'proposal_cards'"
            ).fetchone()

    assert version == proposals.SCHEMA_VERSION
    assert run_table is not None
    assert card_table is not None
    assert hermes_home / "self_improvement" / "proposals.db" == db_path
    assert (hermes_home / "self_improvement" / "proposals.db").exists()
    assert not (fake_home / ".hermes" / "self_improvement" / "proposals.db").exists()


def test_schema_includes_parse_metadata_fields(tmp_path):
    db_path = tmp_path / "proposals.db"

    with proposals.connect(db_path) as conn:
        run_columns = {row[1] for row in conn.execute("PRAGMA table_info(proposal_runs)").fetchall()}
        card_columns = {row[1] for row in conn.execute("PRAGMA table_info(proposal_cards)").fetchall()}

    assert "parse_status" in run_columns
    assert "parse_error" in run_columns
    assert "evidence_bullets" in card_columns
    assert "acceptance_criteria" in card_columns
    assert "proposed_worker_prompt" in card_columns


def test_resolve_project_prong_supports_configured_aliases():
    config = {
        "self_improvement": {
            "proposals": {
                "project_aliases": {"PID-7": "sligo"},
                "projects": {
                    "sligo": {
                        "default_prong": "r1",
                        "prong_aliases": {"PID-7-R1": "r1"},
                        "prongs": {"r1": {"name": "Round 1"}},
                    }
                },
            }
        }
    }

    resolved = proposals.resolve_project_prong("PID-7", "PID-7-R1", config=config)

    assert resolved["project_key"] == "sligo"
    assert resolved["prong_key"] == "r1"
    assert resolved["prong"]["name"] == "Round 1"


def test_ingest_run_and_card_are_idempotent_and_public_records_are_sanitized(tmp_path):
    db_path = tmp_path / "proposals.db"

    with proposals.connect(db_path) as conn:
        run = proposals.ingest_run(
            {
                "project_key": "sligo",
                "prong_key": "r1",
                "idempotency_key": "run-1",
                "source_type": "cron",
                "source_id": "cron-42",
                "raw_input_ref": "/private/raw/cron.md",
                "metadata": {"private": True},
            },
            conn=conn,
        )
        duplicate_run = proposals.ingest_run(
            {
                "project_key": "sligo",
                "prong_key": "r1",
                "idempotency_key": "run-1",
                "source_type": "cron",
            },
            conn=conn,
        )
        card = proposals.ingest_card(
            {
                "run_id": run["id"],
                "idempotency_key": "card-1",
                "title": "Tighten proposal parser",
                "summary": "Improve parser resilience.",
                "tags": ["parser", "sligo"],
                "metadata": {"private": "keep internal"},
            },
            conn=conn,
        )
        duplicate_card = proposals.ingest_card(
            {
                "run_id": run["id"],
                "idempotency_key": "card-1",
                "title": "Changed title should not create another card",
            },
            conn=conn,
        )
        public_card = proposals.get_card(card["id"], conn=conn)
        public_run = proposals.get_run(run["id"], conn=conn)
        run_count = conn.execute("SELECT COUNT(*) FROM proposal_runs").fetchone()[0]
        card_count = conn.execute("SELECT COUNT(*) FROM proposal_cards").fetchone()[0]

    assert duplicate_run["id"] == run["id"]
    assert duplicate_card["id"] == card["id"]
    assert duplicate_card["title"] == "Tighten proposal parser"
    assert run_count == 1
    assert card_count == 1
    assert public_card["tags"] == ["parser", "sligo"]
    assert "idempotency_key" not in public_card
    assert "metadata" not in public_card
    assert public_card["audit_log"][0]["action"] == "ingested"
    assert "raw_input_ref" not in public_run
    assert "metadata" not in public_run


def test_status_decision_outcome_and_worker_link_idempotency(tmp_path):
    db_path = tmp_path / "proposals.db"

    with proposals.connect(db_path) as conn:
        run = proposals.ingest_run(
            {
                "project_key": "sligo",
                "prong_key": "r1",
                "idempotency_key": "run-1",
            },
            conn=conn,
        )
        card = proposals.ingest_card(
            {"run_id": run["id"], "idempotency_key": "card-1", "title": "Ship storage"},
            conn=conn,
        )

        approved = proposals.record_decision(
            card["id"],
            "approved",
            actor="operator",
            reason="Good scope",
            feedback="Prefer small, reversible changes.",
            conn=conn,
        )
        linked = proposals.link_worker_task(
            card["id"], board="discord-board", task_id="t_123", status="ready", url="https://discord/app", conn=conn
        )
        duplicate_link = proposals.link_worker_task(
            card["id"], board="discord-board", task_id="t_123", status="ready", url="https://discord/app", conn=conn
        )
        outcome = proposals.record_outcome(card["id"], status="landed", summary="Tests passed.", conn=conn)

    assert approved["status"] == "approved"
    assert approved["approved_by"] == "operator"
    assert approved["decision_reason"] == "Good scope"
    assert linked["worker_board"] == "discord-board"
    assert linked["worker_task_id"] == "t_123"
    assert len(duplicate_link["audit_log"]) == len(linked["audit_log"])
    assert outcome["outcome_status"] == "landed"
    assert outcome["outcome_summary"] == "Tests passed."


def test_feedback_context_is_compact_project_prong_scoped_and_finds_recurring_preferences(tmp_path):
    db_path = tmp_path / "proposals.db"

    with proposals.connect(db_path) as conn:
        scoped_run = proposals.ingest_run(
            {"project_key": "sligo", "prong_key": "r1", "idempotency_key": "run-scoped"},
            conn=conn,
        )
        other_run = proposals.ingest_run(
            {"project_key": "sligo", "prong_key": "r2", "idempotency_key": "run-other"},
            conn=conn,
        )
        first = proposals.ingest_card(
            {
                "run_id": scoped_run["id"],
                "idempotency_key": "card-approved-1",
                "title": "Approved storage",
                "summary": "Store proposal cards.",
            },
            conn=conn,
        )
        second = proposals.ingest_card(
            {
                "run_id": scoped_run["id"],
                "idempotency_key": "card-approved-2",
                "title": "Approved context",
                "summary": "Summarize feedback.",
            },
            conn=conn,
        )
        rejected = proposals.ingest_card(
            {
                "run_id": scoped_run["id"],
                "idempotency_key": "card-rejected",
                "title": "Huge rewrite",
                "summary": "Rewrite everything at once.",
            },
            conn=conn,
        )
        other = proposals.ingest_card(
            {"run_id": other_run["id"], "idempotency_key": "card-other", "title": "Other prong"},
            conn=conn,
        )

        for card in (first, second):
            proposals.record_decision(
                card["id"],
                "approved",
                actor="operator",
                reason="Good scope",
                feedback="Prefer small, reversible changes.",
                conn=conn,
            )
        proposals.record_outcome(first["id"], status="landed", summary="Merged with tests.", conn=conn)
        proposals.record_decision(
            rejected["id"],
            "rejected",
            actor="operator",
            reason="Too broad",
            feedback="Prefer small, reversible changes.",
            conn=conn,
        )
        proposals.record_decision(other["id"], "approved", actor="operator", reason="Other prong", conn=conn)

        context = proposals.build_feedback_context("sligo", "r1", limit=10, conn=conn)

    approved_titles = {item["title"] for item in context["approved"]}
    rejected_titles = {item["title"] for item in context["rejected"]}
    outcome_titles = {item["title"] for item in context["outcomes"]}

    assert context["project_key"] == "sligo"
    assert context["prong_key"] == "r1"
    assert approved_titles == {"Approved storage", "Approved context"}
    assert rejected_titles == {"Huge rewrite"}
    assert outcome_titles == {"Approved storage"}
    assert "Other prong" not in approved_titles
    assert context["operator_preferences"] == ["Prefer small, reversible changes.", "Good scope"]


def test_cron_json_proposal_output_ingests_cards_and_is_idempotent(tmp_path):
    hermes_home = tmp_path / "hermes-home"
    output_path = hermes_home / "cron" / "output" / "job-1" / "run.md"
    config = {
        "self_improvement": {
            "proposals": {
                "projects": {"sligo": {"default_prong": "r1", "prongs": {"r1": {}}}},
                "cron_jobs": {"job-1": {"project": "sligo", "prong": "r1"}},
            }
        }
    }
    output = "Report\n```json\n" + json.dumps(
        {
            proposals.PROPOSAL_BLOCK_KEY: {
                "parser_version": "1",
                "project": "sligo",
                "prong": "r1",
                "proposals": [
                    {
                        "title": "Add ingestion hook",
                        "summary": "Parse structured proposal blocks.",
                        "body": "Wire cron output into storage.",
                        "rationale": "Keeps review queue current.",
                        "evidence_bullets": ["Cron emitted a structured block.", "Parser reads it."],
                        "acceptance_criteria": ["API exposes evidence.", "Drawer shows prompt."],
                        "proposed_worker_prompt": "Implement the ingestion hook and test it.",
                        "expected_outcome": "One card appears.",
                        "priority": "high",
                        "tags": ["cron"],
                    }
                ],
            }
        }
    ) + "\n```"

    with patch.dict("os.environ", {"HERMES_HOME": str(hermes_home)}):
        first = proposals.ingest_cron_proposal_output(
            {"id": "job-1", "name": "Daily proposals"}, output, source_output_path=output_path, config=config
        )
        second = proposals.ingest_cron_proposal_output(
            {"id": "job-1", "name": "Daily proposals"}, output, source_output_path=output_path, config=config
        )
        with proposals.connect() as conn:
            run = proposals.sanitize_run(proposals.decode_row(conn.execute("SELECT * FROM proposal_runs").fetchone()))
            cards = [proposals.sanitize_card(proposals.decode_row(row)) for row in conn.execute("SELECT * FROM proposal_cards").fetchall()]

    assert first["parse_status"] == "parsed"
    assert second["run_id"] == first["run_id"]
    assert run["source_type"] == "cron"
    assert run["parser_name"] == proposals.STRUCTURED_CRON_PARSER
    assert run["parse_status"] == "parsed"
    assert run["source_output_ref"] != str(output_path)
    with patch.dict("os.environ", {"HERMES_HOME": str(hermes_home)}):
        assert proposals.resolve_source_output_ref(run["source_output_ref"]) == output_path.resolve()
    assert len(cards) == 1
    assert cards[0]["title"] == "Add ingestion hook"
    assert cards[0]["tags"] == ["cron"]
    assert cards[0]["evidence_bullets"] == ["Cron emitted a structured block.", "Parser reads it."]
    assert cards[0]["acceptance_criteria"] == ["API exposes evidence.", "Drawer shows prompt."]
    assert cards[0]["proposed_worker_prompt"] == "Implement the ingestion hook and test it."
    assert cards[0]["source_output_ref"] == run["source_output_ref"]
    assert cards[0]["audit_log"][0]["action"] == "ingested"
    assert "metadata" not in cards[0]


def test_cron_malformed_proposal_json_records_parse_failure(tmp_path):
    hermes_home = tmp_path / "hermes-home"
    config = {
        "self_improvement": {
            "proposals": {
                "projects": {"sligo": {"default_prong": "r1", "prongs": {"r1": {}}}},
                "cron_jobs": {"job-1": {"project": "sligo", "prong": "r1"}},
            }
        }
    }

    with patch.dict("os.environ", {"HERMES_HOME": str(hermes_home)}):
        result = proposals.ingest_cron_proposal_output(
            {"id": "job-1"},
            f"```json\n{{\"{proposals.PROPOSAL_BLOCK_KEY}\": {{\"proposals\": [}}\n```",
            config=config,
        )
        with proposals.connect() as conn:
            run = conn.execute("SELECT * FROM proposal_runs").fetchone()
            card_count = conn.execute("SELECT COUNT(*) FROM proposal_cards").fetchone()[0]

    assert result["parse_status"] == "failed"
    assert run["status"] == "failed"
    assert run["parse_error"] == "proposal JSON block is malformed"
    assert card_count == 0


def test_cron_feedback_prompt_section_is_configured_and_scoped(tmp_path):
    db_path = tmp_path / "proposals.db"
    config = {
        "self_improvement": {
            "proposals": {
                "projects": {"sligo": {"default_prong": "r1", "prongs": {"r1": {}, "r2": {}}}},
                "cron_jobs": {"Daily proposals": {"project": "sligo", "prong": "r1"}},
                "feedback_context_limit": 10,
            }
        }
    }
    with proposals.connect(db_path) as conn:
        run = proposals.ingest_run({"project_key": "sligo", "prong_key": "r1", "idempotency_key": "run-1"}, conn=conn)
        other_run = proposals.ingest_run({"project_key": "sligo", "prong_key": "r2", "idempotency_key": "run-2"}, conn=conn)
        approved = proposals.ingest_card({"run_id": run["id"], "idempotency_key": "card-1", "title": "Small fix"}, conn=conn)
        other = proposals.ingest_card({"run_id": other_run["id"], "idempotency_key": "card-2", "title": "Other prong"}, conn=conn)
        proposals.record_decision(approved["id"], "approved", feedback="Prefer scoped fixes.", conn=conn)
        proposals.record_decision(other["id"], "rejected", feedback="Do not include.", conn=conn)

    with patch.object(proposals, "get_db_path", return_value=db_path):
        section = proposals.build_cron_feedback_prompt_section({"id": "job-9", "name": "Daily proposals"}, config=config)

    assert "Self-Improvement Proposal Feedback Context" in section
    assert "Small fix" in section
    assert "Prefer scoped fixes." in section
    assert "Other prong" not in section
    assert proposals.PROPOSAL_BLOCK_KEY in section
    assert "evidence_bullets" in section
    assert "acceptance_criteria" in section
    assert "proposed_worker_prompt" in section


def test_invalid_status_and_json_are_rejected(tmp_path):
    db_path = tmp_path / "proposals.db"
    unserializable = object()

    with proposals.connect(db_path) as conn:
        with pytest.raises(ValueError, match="Invalid proposal run status"):
            proposals.ingest_run(
                {
                    "project_key": "sligo",
                    "prong_key": "r1",
                    "idempotency_key": "run-bad-status",
                    "status": "unknown",
                },
                conn=conn,
            )

        run = proposals.ingest_run(
            {"project_key": "sligo", "prong_key": "r1", "idempotency_key": "run-good"},
            conn=conn,
        )
        with pytest.raises(TypeError):
            proposals.ingest_card(
                {
                    "run_id": run["id"],
                    "idempotency_key": "card-bad-json",
                    "title": "Bad JSON",
                    "metadata": {"bad": unserializable},
                },
                conn=conn,
            )
