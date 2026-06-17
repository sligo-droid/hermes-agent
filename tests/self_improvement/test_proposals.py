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


def _github_pr_amend_card() -> dict:
    return {
        "proposal_id": "github-pr-amend-123",
        "project": "reserve-index-dtf",
        "prong": "github-pr-amend",
        "title": "GitHub PR amend: reserve-protocol/reserve-index-dtf#182",
        "summary": "Route the accepted review request through the worker board.",
        "priority": "high",
    }


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


def test_project_context_preserves_explicit_card_context(monkeypatch, tmp_path):
    project = tmp_path / "reserve-index-dtf"
    project.mkdir()

    monkeypatch.setattr(
        discord_publish,
        "load_config_readonly",
        lambda: {"discord": {"channel_cwds": {"12345": str(project)}}},
    )
    monkeypatch.setattr(discord_publish, "_project_mapping_for_key", lambda _project: {})

    context = discord_publish._project_context(
        {
            "project": "reserve-index-dtf",
            "project_context": {
                "github_pr_target_repo": "sligo-droid/reserve-index-dtf",
                "base_branch": "feat/irrevocable-fee-recipients",
                "github_pr_amend": {"head_sha": "19a1d0b"},
            },
        },
        "12345",
    )

    assert context["project_path"] == str(project.resolve())
    assert context["github_pr_target_repo"] == "sligo-droid/reserve-index-dtf"
    assert context["base_branch"] == "feat/irrevocable-fee-recipients"
    assert context["github_pr_amend"] == {"head_sha": "19a1d0b"}


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


def test_self_improvement_discord_approval_embed_links_worker_board():
    card = {
        "proposal_id": "hermes-worker-link-123",
        "project": "hermes",
        "prong": "daily-retrospective",
        "title": "Fix worker board links",
        "summary": "Approved cron embeds should open the specific worker board.",
        "priority": "high",
    }

    embed = discord_publish._feature_embed(card, "https://sligo.sligolabs.com/workers/1512960023947378698")

    assert embed["url"] == "https://sligo.sligolabs.com/workers/1512960023947378698"
    assert embed["url"].rstrip("/") != "https://sligo.sligolabs.com/workers"
    fields = {field["name"]: field["value"] for field in embed["fields"]}
    assert fields["Worker board"] == "https://sligo.sligolabs.com/workers/1512960023947378698"


def test_self_improvement_discord_publish_posts_thread_summary_embed(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "hermes-home" / "kanban"))
    monkeypatch.setenv("HERMES_PUBLIC_KANBAN_BASE_URL", "https://example.test")
    monkeypatch.setattr(discord_publish, "_project_mapping_for_key", lambda _project: {})
    monkeypatch.setattr(discord_publish, "load_config_readonly", lambda: {})

    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_roles import DISCORD_WORKER_META_KEY
    from tools import discord_tool

    calls = []

    def fake_request(method, path, token, **kwargs):
        calls.append({"method": method, "path": path, "token": token, "kwargs": kwargs})
        if method == "POST" and path == "/channels/123/messages":
            return {"id": "555", "guild_id": "999"}
        if method == "POST" and path == "/channels/123/messages/555/threads":
            return {"id": "777", "guild_id": "999"}
        if method == "POST" and path == "/channels/777/messages":
            return {"id": "888"}
        return {}

    monkeypatch.setattr(discord_tool, "_get_bot_token", lambda: "tok")
    monkeypatch.setattr(discord_tool, "_discord_request", fake_request)

    card = _github_pr_amend_card()

    route = discord_publish.publish_approved_proposal(card, channel_id="123")

    assert route is not None
    assert route.thread_id == "777"
    assert route.top_level_message_id == "555"
    assert route.summary_message_id == "888"
    assert route.metadata()["discord_summary_message_id"] == "888"

    assert any(
        call["method"] == "POST" and call["path"] == "/channels/777/messages"
        for call in calls
    )
    assert any(
        call["method"] == "PUT"
        and call["path"] == "/channels/777/messages/888/reactions/%F0%9F%91%80/@me"
        for call in calls
    )

    worker = kanban_db.read_board_metadata(route.board)[DISCORD_WORKER_META_KEY]
    assert worker["summary_message_id"] == "888"
    assert worker["source_message_id"] == "555"

    state_path = tmp_path / "hermes-home" / "gateway" / "discord_project_summaries.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    handle = state["_feature_summaries"]["999:777:555"]
    assert handle["message_id"] == "888"
    assert handle["source_message_id"] == "555"
    assert handle["summary_channel_id"] == "777"
    assert handle["kanban_board"] == {"slug": route.board, "public_url": route.board_public_url}


def test_self_improvement_discord_thread_summary_recovers_stale_message(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "hermes-home" / "kanban"))
    monkeypatch.setenv("HERMES_PUBLIC_KANBAN_BASE_URL", "https://example.test")
    monkeypatch.setattr(discord_publish, "_project_mapping_for_key", lambda _project: {})
    monkeypatch.setattr(discord_publish, "load_config_readonly", lambda: {})

    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_roles import DISCORD_WORKER_META_KEY
    from tools import discord_tool

    calls = []

    def fake_request(method, path, token, **kwargs):
        calls.append({"method": method, "path": path, "token": token, "kwargs": kwargs})
        if method == "PATCH" and path == "/channels/777/messages/stale-summary":
            raise RuntimeError("Discord API error 404: Unknown Message")
        if method == "POST" and path == "/channels/777/messages":
            return {"id": "fresh-summary"}
        return {}

    monkeypatch.setattr(discord_tool, "_get_bot_token", lambda: "tok")
    monkeypatch.setattr(discord_tool, "_discord_request", fake_request)

    card = _github_pr_amend_card()
    route = discord_publish.DiscordApprovalRoute(
        channel_id="123",
        top_level_message_id="555",
        thread_id="777",
        thread_url="https://discord.test/thread/777",
        board="discord-777",
        board_public_url="https://example.test/workers/discord-777",
        guild_id="999",
        summary_message_id="stale-summary",
    )
    route = discord_publish.ensure_approval_route_board(card, route)
    route = discord_publish.ensure_thread_feature_summary(card, route)

    assert route is not None
    assert route.error == ""
    assert route.summary_message_id == "fresh-summary"
    assert [call["method"] for call in calls[:2]] == ["PATCH", "POST"]
    assert any(
        call["method"] == "PUT"
        and call["path"] == "/channels/777/messages/fresh-summary/reactions/%F0%9F%91%80/@me"
        for call in calls
    )

    worker = kanban_db.read_board_metadata(route.board)[DISCORD_WORKER_META_KEY]
    assert worker["summary_message_id"] == "fresh-summary"
    assert worker["source_message_id"] == "555"


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
            "acceptance_criteria": [
                "Approved proposal starts in planning and creates dev tickets.",
                "Reviewer loop runs before completion.",
            ],
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
    assert worker["criteria"] == [
        "Approved proposal starts in planning and creates dev tickets.",
        "Reviewer loop runs before completion.",
    ]
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
    assert payload["acceptance_criteria"] == [
        "Approved proposal starts in planning and creates dev tickets.",
        "Reviewer loop runs before completion.",
    ]
    instructions = "\n".join(payload["planner_instructions"])
    assert "ticket-specific acceptance criteria" in instructions
    assert "Definition of Done, Success means, and Stop when" in instructions
    assert "Do not copy the board-level acceptance_criteria wholesale" in instructions


def test_self_improvement_acceptance_criteria_does_not_fallback_to_task_brief():
    card = {
        "title": "Improve worker board approvals",
        "summary": "Approved self-improvement proposal should use the full worker flow.",
        "body": "Users can approve a proposal and see planner-created tickets move through review.",
        "rationale": "Direct dev execution skipped planning and reviewer loop expectations.",
        "kanban_task": {
            "title": "Route approved proposal through planner",
            "body": "Implement the approval activation so the Discord worker board starts in planning and produces dev tickets.",
        },
    }

    assert discord_publish._acceptance_criteria(card) == []


def test_valid_pid_proposal_run_is_accepted_and_gets_stable_id():
    payload = _fixture("proposal_run_pid_valid.json")

    normalized = validate_proposal_run(payload)

    assert normalized["contract_version"] == CONTRACT_VERSION
    assert normalized["project"] == "pid"
    assert normalized["prong"] == "airflow_scraper_doctor"
    assert normalized["cards"][0]["proposal_id"].startswith("pid-airflow_scraper_doctor-")
    assert validate_proposal_run(payload)["cards"][0]["proposal_id"] == normalized["cards"][0]["proposal_id"]
    assert normalized["cards"][0]["evidence_basis"] == {
        "type": "source_static_log",
        "summary": "Supported by PID Airflow scraper cron logs; no authenticated live/browser dogfood verification is claimed.",
        "missing_live_evidence": [],
    }


def test_blocked_live_stream_source_backed_card_names_missing_live_evidence():
    normalized = validate_proposal_run(_fixture("proposal_run_pid_blocked_live_source_backed.json"))

    card = normalized["cards"][0]
    basis = card["evidence_basis"]
    assert "safe admin credentials were unavailable" in normalized["human_markdown"]
    assert "authenticated admin dogfood completed" not in normalized["human_markdown"].lower()
    assert "live-verified" not in normalized["human_markdown"].lower()
    assert basis["type"] == "source_static_log"
    assert basis["missing_live_evidence"] == [
        "authenticated admin browser dogfood blocked because safe admin credentials were unavailable"
    ]
    combined = " ".join([card["summary"], card["body"], card["rationale"], basis["summary"]])
    assert "safe admin credentials were unavailable" in combined
    assert "live-verified" not in combined.lower()
    assert "authenticated live/browser dogfood is explicitly not claimed" in basis["summary"]


def test_live_claim_requires_live_browser_evidence_basis():
    payload = _fixture("proposal_run_pid_blocked_live_source_backed.json")
    payload["cards"][0]["summary"] = "RECOMMENDED after authenticated admin dogfood completed."

    with pytest.raises(ProposalValidationError) as excinfo:
        validate_proposal_run(payload)

    assert "evidence_basis.type must be live_browser" in str(excinfo.value)


def test_human_markdown_live_claim_requires_live_browser_card_evidence_basis():
    payload = _fixture("proposal_run_pid_blocked_live_source_backed.json")
    payload["human_markdown"] = "Status: RECOMMENDED after authenticated admin dogfood completed."

    with pytest.raises(ProposalValidationError) as excinfo:
        validate_proposal_run(payload)

    assert "human_markdown must not imply authenticated live/browser dogfood verification" in str(excinfo.value)


def test_blocked_missing_live_basis_must_name_missing_live_evidence():
    payload = _fixture("proposal_run_pid_blocked_live_source_backed.json")
    payload["cards"][0]["evidence_basis"] = {
        "type": "blocked_missing_live",
        "summary": "Live verification was blocked.",
        "missing_live_evidence": [],
    }

    with pytest.raises(ProposalValidationError) as excinfo:
        validate_proposal_run(payload)

    assert "missing_live_evidence" in str(excinfo.value)


def test_missing_live_evidence_must_be_array_not_object():
    payload = _fixture("proposal_run_pid_blocked_live_source_backed.json")
    payload["cards"][0]["evidence_basis"]["missing_live_evidence"] = {
        "reason": "safe admin credentials were unavailable"
    }

    with pytest.raises(ProposalValidationError) as excinfo:
        validate_proposal_run(payload)

    assert "cards[0].evidence_basis.missing_live_evidence must be an array" in str(excinfo.value)


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
    assert "`evidence_basis`" in guidance
    assert "`missing_live_evidence` must be an array of non-empty strings" in guidance
    assert "use `[]` when no live evidence is missing" in guidance
    assert "`source_static_log`, `live_browser`, or `blocked_missing_live`" in guidance
    assert "INSUFFICIENT_EVIDENCE" in guidance
    assert "safe admin credentials unavailable" in guidance
    assert "source_excerpts` alone are not a live verification claim" in guidance
    assert "must not imply authenticated live dogfood occurred" in guidance
    assert "card `summary` <= 500 chars" in guidance
    assert "card `body` <= 6000 chars" in guidance
    assert "`kanban_task.body` <= 6000 chars" in guidance
    assert "Do not put audit notes" in guidance


def test_cron_proposal_guidance_downgrades_no_final_coding_worker_evidence():
    guidance = build_cron_proposal_guidance("pid", "admin_dogfood_ux_bugfix")

    assert "coding-worker streams with empty or absent final text" in guidance
    assert "degraded, non-authoritative evidence" in guidance
    assert "empty coding-worker final must not by itself satisfy a delegated worker evidence floor" in guidance
    assert "top-level `evidence_status` and `failure_class`" in guidance
    assert "`no_final_metadata.classification`" in guidance
    assert "`no_final_metadata.clean_committed_branch`" in guidance
    assert "`no_final_metadata.commit`" in guidance
    assert "no recoverable artifact" in guidance
    assert "do not cite it as delegated evidence" in guidance
    assert "`evidence_status: recoverable_degraded`" in guidance
    assert "`local_commit_detected` and `clean_committed_branch`" in guidance
    assert "recoverable artifact metadata as source/static/log evidence" in guidance
    assert "still do not claim the empty final itself is authoritative" in guidance
    assert "Do not reflexively redo parent verification" in guidance
    assert "redo direct verification only when the artifact is small enough" in guidance
