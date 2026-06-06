import json
from pathlib import Path

import pytest

from hermes_cli.config import DEFAULT_CONFIG, load_config
from self_improvement import discord_publish
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
    assert "discord_channel_id" in pid
    assert set(pid["prongs"]) >= {
        "airflow_scraper_doctor",
        "admin_dogfood_ux_bugfix",
        "invisible_technical_recommendations",
        "visible_ui_ux_recommendations",
    }
    assert DEFAULT_CONFIG["self_improvement"]["projects"]["pid"]["prongs"]["airflow_scraper_doctor"]["max_cards_per_run"] == 5


def test_self_improvement_discord_channel_lookup_accepts_aliases(monkeypatch):
    for key in ("discord_channel_id", "discord_project_channel_id", "project_discord_channel_id"):
        monkeypatch.setattr(
            discord_publish,
            "load_config_readonly",
            lambda key=key: {"self_improvement": {"projects": {"pid": {key: "12345"}}}},
        )
        assert discord_publish.configured_project_channel_id("pid") == "12345"


def test_self_improvement_discord_channel_lookup_falls_back_to_project_mapping(monkeypatch):
    import hermes_state

    closed = []

    class FakeSessionDB:
        def list_discord_project_mappings(self):
            return [
                {
                    "project_key": "pid",
                    "project_name": "PID",
                    "channel_id": "424242",
                    "project_path": "/home/droid/.hermes/workspace/PID",
                    "github_url": "https://github.com/sligo-labs/PID",
                    "source": "manual",
                }
            ]

        def close(self):
            closed.append(True)

    monkeypatch.setattr(
        discord_publish,
        "load_config_readonly",
        lambda: {"self_improvement": {"projects": {"pid": {}}}},
    )
    monkeypatch.setattr(hermes_state, "SessionDB", FakeSessionDB)

    assert discord_publish.configured_project_channel_id("PID") == "424242"
    assert discord_publish._project_context(
        {"project": "PID", "prong": "visible_ui_ux_recommendations"},
        "424242",
    ) == {
        "project_name": "PID",
        "project_path": "/home/droid/.hermes/workspace/PID",
        "project_github_url": "https://github.com/sligo-labs/PID",
        "project_channel_id": "424242",
        "project_mapping_source": "manual",
        "project_mapping_resolved": True,
        "self_improvement_project": "PID",
        "self_improvement_prong": "visible_ui_ux_recommendations",
    }
    assert closed == [True, True]


def test_self_improvement_project_context_falls_back_to_channel_cwd(monkeypatch, tmp_path):
    project = tmp_path / "hermes"
    project.mkdir()

    monkeypatch.setattr(
        discord_publish,
        "load_config_readonly",
        lambda: {
            "self_improvement": {"projects": {"hermes": {}}},
            "discord": {"channel_cwds": {"12345": str(project)}},
        },
    )
    monkeypatch.setattr(discord_publish, "_project_mapping_for_key", lambda _project: {})

    assert discord_publish._project_context(
        {"project": "hermes", "prong": "daily-retrospective"},
        "12345",
    ) == {
        "project_name": "hermes",
        "project_path": str(project.resolve()),
        "project_channel_id": "12345",
        "project_mapping_source": "configured_channel_cwd",
        "project_mapping_resolved": True,
        "self_improvement_project": "hermes",
        "self_improvement_prong": "daily-retrospective",
    }


def test_self_improvement_project_context_uses_channel_cwd_when_mapping_has_no_path(monkeypatch, tmp_path):
    project = tmp_path / "hermes"
    project.mkdir()

    monkeypatch.setattr(
        discord_publish,
        "load_config_readonly",
        lambda: {"discord": {"channel_cwds": {"12345": str(project)}}},
    )
    monkeypatch.setattr(
        discord_publish,
        "_project_mapping_for_key",
        lambda _project: {
            "project_name": "Hermes",
            "channel_id": "12345",
            "source": "session_project_mapping",
        },
    )

    assert discord_publish._project_context({"project": "hermes"}, "12345") == {
        "project_name": "hermes",
        "project_path": str(project.resolve()),
        "project_channel_id": "12345",
        "project_mapping_source": "configured_channel_cwd",
        "project_mapping_resolved": True,
        "self_improvement_project": "hermes",
        "self_improvement_prong": "",
    }


def test_self_improvement_discord_initial_reaction_url_encodes_unicode(monkeypatch):
    from tools import discord_tool

    calls = []

    def fake_request(method, path, token, **kwargs):
        calls.append({"method": method, "path": path, "token": token, "kwargs": kwargs})

    monkeypatch.setattr(discord_tool, "_discord_request", fake_request)

    discord_publish._add_reaction("tok", "123", "456", "👀")

    assert calls == [
        {
            "method": "PUT",
            "path": "/channels/123/messages/456/reactions/%F0%9F%91%80/@me",
            "token": "tok",
            "kwargs": {},
        }
    ]


def test_self_improvement_activation_uses_planner_flow_with_board_criteria(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "hermes"))
    monkeypatch.setenv("HERMES_PUBLIC_KANBAN_BASE_URL", "https://example.test")
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db

    card = {
        "proposal_id": "pid-visible-123",
        "project": "pid",
        "prong": "visible_ui_ux_recommendations",
        "title": "Improve worker board approvals",
        "summary": "Approved self-improvement proposal should use the full worker flow.",
        "body": "Users can approve a proposal and see planner-created tickets move through review.",
        "rationale": "Direct dev execution skipped planning and reviewer loop expectations.",
        "kanban_task": {
            "title": "Route approved proposal through planner",
            "body": "Implement the approval activation so the Discord worker board starts in planning and produces dev tickets.",
        },
    }
    route = discord_publish.DiscordApprovalRoute(
        channel_id="555",
        top_level_message_id="666",
        thread_id="777",
        thread_url="https://discord.com/channels/999/777",
        board="discord-777-m-666",
        board_public_url="https://example.test/workers/discord-777-m-666",
        guild_id="999",
    )
    dwb.ensure_discord_thread_board(
        thread_id=route.thread_id,
        chat_id=route.channel_id,
        guild_id=route.guild_id,
        parent_channel_id=route.channel_id,
        initial_request=discord_publish._initial_request(card),
        project_context={"project_name": "pid"},
        request_id=route.top_level_message_id,
        source_message_id=route.top_level_message_id,
        board_slug=route.board,
    )

    activated = discord_publish.activate_approved_proposal(card, route)

    assert activated is not None
    assert activated.board == "discord-777-m-666"
    worker = kanban_db.read_board_metadata(activated.board)[dwb.DISCORD_WORKER_META_KEY]
    assert worker["goal_status"] == "active"
    assert worker["phase"] == "planning"
    assert worker["execution_mode"] == "kanban_pipeline"
    assert worker["criteria"]
    assert "starts in planning" in worker["criteria"][0]
    assert "reviewer loop" in worker["criteria"][0]
    assert worker["latest_planner_task_id"]

    conn = kanban_db.connect(board=activated.board)
    try:
        tasks = kanban_db.list_tasks(conn, include_archived=False)
    finally:
        conn.close()
    assert len(tasks) == 1
    task = tasks[0]
    assert task.assignee == "planner"
    assert task.created_by == "self-improvement"
    assert worker["latest_planner_task_id"] == task.id
    assert "dev" not in task.assignee
    payload = json.loads(task.body or "{}")
    assert payload["role"] == "planner"
    assert payload["acceptance_criteria"] == worker["criteria"]
    assert "starts in planning" in payload["acceptance_criteria"][0]
    instructions = "\n".join(payload["planner_instructions"])
    assert "ticket-specific acceptance criteria" in instructions
    assert "Definition of Done, Success means, and Stop when" in instructions
    assert "Do not copy the board-level acceptance_criteria wholesale" in instructions


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
    assert "card `summary` <= 500 chars" in guidance
    assert "card `body` <= 6000 chars" in guidance
    assert "`kanban_task.body` <= 6000 chars" in guidance
    assert "Do not put audit notes" in guidance
