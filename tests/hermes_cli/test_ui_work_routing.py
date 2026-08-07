from __future__ import annotations

import copy
import json

import pytest

from hermes_cli.config import DEFAULT_CONFIG
from hermes_cli.ui_work_routing import (
    resolve_ui_work_route,
    ui_specialist_skills_for_model_tier,
)


def _cfg():
    return copy.deepcopy(DEFAULT_CONFIG)


def test_ui_specialist_skills_vary_by_canonical_model_tier():
    assert ui_specialist_skills_for_model_tier("trivial") == ("taste-skill",)
    assert ui_specialist_skills_for_model_tier("basic") == (
        "taste-skill",
        "claude-design",
    )
    for tier in ("intermediate", "advanced"):
        assert ui_specialist_skills_for_model_tier(tier) == (
            "taste-skill",
            "claude-design",
            "popular-web-designs",
        )


def test_ui_specialist_unknown_or_omitted_tier_keeps_all_skills():
    expected = ("taste-skill", "claude-design", "popular-web-designs")
    assert ui_specialist_skills_for_model_tier(None) == expected
    assert ui_specialist_skills_for_model_tier("") == expected
    assert ui_specialist_skills_for_model_tier("third-party-ultra") == expected


def test_route_metadata_uses_canonical_model_tier_without_changing_runtime():
    decision = resolve_ui_work_route(
        _cfg(),
        task="Polish the responsive dashboard layout.",
        backend="opencode",
        model_tier="trivial",
        route_decision="ui_visual_specialist",
    )

    metadata = decision.metadata()
    assert metadata["model_tier"] == "trivial"
    assert "worker_tier" not in metadata
    assert metadata["recommended_skills"] == ["taste-skill"]
    assert decision.backend == "opencode"
    assert decision.provider == ""
    assert decision.model == ""


@pytest.mark.parametrize(
    "task",
    [
        "Implement Command Center card footer visuals and responsive styling.",
        "Polish Command Center card footer visuals and responsive styling.",
        "Redesign Command Center card footer visuals and responsive styling.",
    ],
)
def test_explicit_visual_route_selects_behavioral_profile(task):
    decision = resolve_ui_work_route(
        _cfg(),
        task=task,
        backend="codex",
        route_decision={
            "route": "ui_visual_specialist",
            "confidence": 0.91,
            "rationale": "visual implementation",
        },
    )

    assert decision.matched is True
    assert decision.enabled is True
    assert decision.reason == "orchestrator route selected ui visual specialist"
    assert decision.route_decision_source == "orchestrator"
    assert decision.route_decision_confidence == 0.91
    assert decision.route_decision_rationale == "visual implementation"
    assert decision.selected_route == "ui_visual_specialist"
    assert decision.selected_provider == ""
    assert decision.selected_model == ""
    assert decision.metadata()["recommended_skills"] == [
        "taste-skill",
        "claude-design",
        "popular-web-designs",
    ]
    assert decision.advisory_matched is True
    assert "visual ui work" in decision.advisory_reason
    assert decision.provider == ""
    assert decision.model == ""
    assert decision.backend == "codex"
    assert decision.backend_config == {}
    assert decision.visual_advisor_tier == "standard"


def test_visual_specialist_preserves_configured_opencode_backend():
    decision = resolve_ui_work_route(
        _cfg(),
        task="Implement Command Center card footer visuals and responsive styling.",
        backend="opencode",
        route_decision={
            "route": "ui_visual_specialist",
            "confidence": 0.91,
            "rationale": "visual implementation",
        },
    )

    assert decision.matched is True
    assert decision.selected_route == "ui_visual_specialist"
    assert decision.backend == "opencode"
    assert decision.selected_provider == ""
    assert decision.selected_model == ""
    assert decision.provider == ""
    assert decision.model == ""
    assert decision.backend_config == {}


def test_visual_specialist_accepts_json_encoded_route_decision():
    decision = resolve_ui_work_route(
        _cfg(),
        task="Implement Command Center card footer visuals and responsive styling.",
        backend="codex",
        route_decision=json.dumps(
            {
                "route": "ui_visual_specialist",
                "source": "orchestrator",
                "confidence": 0.97,
                "rationale": "Command Center UI task",
            }
        ),
    )

    assert decision.matched is True
    assert decision.reason == "orchestrator route selected ui visual specialist"
    assert decision.route_decision == "ui_visual_specialist"
    assert decision.route_decision_source == "orchestrator"
    assert decision.route_decision_confidence == 0.97
    assert decision.route_decision_rationale == "Command Center UI task"
    assert decision.selected_route == "ui_visual_specialist"
    assert decision.selected_provider == ""
    assert decision.selected_model == ""


def test_visual_specialist_accepts_advanced_advisor_tier():
    decision = resolve_ui_work_route(
        _cfg(),
        task="Design a novel high-impact Command Center workspace hierarchy.",
        route_decision={
            "route": "ui_visual_specialist",
            "visual_advisor_tier": "advanced",
            "rationale": "new product hierarchy with meaningful alternatives",
        },
    )

    assert decision.selected_route == "ui_visual_specialist"
    assert decision.visual_advisor_tier == "advanced"
    assert decision.metadata()["visual_advisor_tier"] == "advanced"


def test_visual_specialist_rejects_unknown_advisor_tier():
    decision = resolve_ui_work_route(
        _cfg(),
        task="Polish the dashboard.",
        route_decision={
            "route": "ui_visual_specialist",
            "visual_advisor_tier": "premium",
        },
    )

    assert decision.selected_route == "default_coding_worker"
    assert "visual_advisor_tier must be" in decision.error


def test_tui_terminal_rendering_work_does_not_route():
    decision = resolve_ui_work_route(
        _cfg(),
        title="Hermes TUI terminal rendering follow-up",
        task="Fix TUI repaint clipping in the terminal session transcript.",
    )

    assert decision.matched is False
    assert "negative keyword" in decision.advisory_reason


def test_review_or_look_at_ui_does_not_route():
    decision = resolve_ui_work_route(
        _cfg(),
        task="Review the Command Center UI and look at the page layout for issues.",
    )

    assert decision.matched is False
    assert "negative keyword" in decision.advisory_reason


def test_explicit_ui_route_overrides_review_keyword_veto():
    decision = resolve_ui_work_route(
        _cfg(),
        task="Review comments asked us to implement visual polish on the dashboard.",
        route_decision={
            "route": "ui_visual_specialist",
            "confidence": 0.8,
            "rationale": "review feedback requires visual implementation",
        },
    )

    assert decision.matched is True
    assert decision.enabled is True
    assert decision.selected_route == "ui_visual_specialist"
    assert decision.selected_provider == ""
    assert decision.route_decision_source == "orchestrator"
    assert decision.route_decision_confidence == 0.8
    assert decision.route_decision_rationale == "review feedback requires visual implementation"
    assert decision.advisory_matched is True
    assert "visual ui work" in decision.advisory_reason


def test_visual_keywords_without_route_select_automatic_standard_advisor():
    decision = resolve_ui_work_route(
        _cfg(),
        task="Implement responsive dashboard card visual polish.",
    )

    assert decision.matched is True
    assert decision.selected_route == "ui_visual_specialist"
    assert decision.metadata()["recommended_skills"] == [
        "taste-skill",
        "claude-design",
        "popular-web-designs",
    ]
    assert decision.selected_provider == ""
    assert decision.selected_model == ""
    assert decision.route_decision_source == "deterministic_explicit_visual"
    assert decision.route_decision_confidence is None
    assert decision.advisory_matched is True
    assert decision.visual_advisor_tier == "standard"
    assert "visual ui work" in decision.advisory_reason


def test_command_center_polish_smoke_selects_automatic_standard_advisor():
    decision = resolve_ui_work_route(
        _cfg(),
        task="Smoke-test UI specialist route on Command Center polish.",
    )

    assert decision.matched is True
    assert decision.selected_route == "ui_visual_specialist"
    assert decision.advisory_matched is True
    assert "visual ui work" in decision.advisory_reason
    assert "polish" in decision.advisory_reason


def test_non_visual_command_center_smoke_still_does_not_route():
    decision = resolve_ui_work_route(
        _cfg(),
        task="Smoke-test Command Center backend API routing.",
    )

    assert decision.matched is False
    assert decision.advisory_matched is False
    assert "negative keyword" in decision.advisory_reason


def test_deterministic_navigation_edit_stays_on_default_coding_route():
    decision = resolve_ui_work_route(
        _cfg(),
        task=(
            "Remove the Briefing Studio tab from the sidebar and move Confidence "
            "directly beneath Races."
        ),
    )

    assert decision.selected_route == "default_coding_worker"
    assert decision.advisory_matched is False
    assert decision.visual_advisor_tier == "standard"


@pytest.mark.parametrize(
    "task",
    [
        "Add exactly 8px spacing between the dashboard cards.",
        "Style the dashboard button color exactly #112233.",
        "Set the card width to 320px.",
    ],
)
def test_exact_visual_property_edit_stays_on_default_coding_route(task):
    decision = resolve_ui_work_route(_cfg(), task=task)

    assert decision.selected_route == "default_coding_worker"
    assert decision.advisory_matched is False
    assert decision.advisory_reason == "deterministic exact visual value"


@pytest.mark.parametrize(
    "task",
    [
        "Polish the responsive dashboard hierarchy and use exactly 8px spacing between cards.",
        (
            "Redesign the dashboard layout, set the card width to 320px, and improve "
            "responsive hierarchy."
        ),
    ],
)
def test_exact_value_does_not_suppress_subjective_visual_work(task):
    decision = resolve_ui_work_route(_cfg(), task=task)

    assert decision.selected_route == "ui_visual_specialist"
    assert decision.advisory_matched is True
    assert decision.visual_advisor_tier == "standard"


def test_negative_backend_only_overrides_ui_keyword():
    decision = resolve_ui_work_route(
        _cfg(),
        task="Backend-only API cleanup; no frontend or UI changes.",
        context="The word dashboard appears in an API response fixture.",
    )

    assert decision.matched is False
    assert "negative keyword" in decision.advisory_reason


def test_backend_api_work_with_ui_context_does_not_route():
    decision = resolve_ui_work_route(
        _cfg(),
        task="Implement backend API pagination for the application UI context.",
        context="The frontend page will consume this endpoint later.",
    )

    assert decision.matched is False
    assert "negative keyword" in decision.advisory_reason


def test_visual_task_is_not_vetoed_by_backend_constraints_in_context():
    decision = resolve_ui_work_route(
        _cfg(),
        task=(
            "Implement the Race page visual: replace the General chart with an "
            "equally sized panel and center the message horizontally and vertically."
        ),
        context=(
            "Inspect existing backend fields and do not invent schema or API changes."
        ),
    )

    assert decision.matched is True
    assert decision.selected_route == "ui_visual_specialist"
    assert decision.route_decision_source == "deterministic_explicit_visual"
    assert decision.advisory_matched is True
    assert "visual ui work" in decision.advisory_reason


@pytest.mark.parametrize(
    "task",
    [
        "Implement Command Center data normalization.",
        "Implement dashboard state routing.",
        "Implement Command Center config plumbing.",
        "Implement Command Center backend API app-state work.",
        "Implement Command Center page layout wiring.",
        "Implement Command Center user interface.",
        "Build app interface.",
        "Implement web interface plumbing.",
    ],
)
def test_broad_command_center_dashboard_work_without_visual_intent_does_not_route(task):
    decision = resolve_ui_work_route(_cfg(), task=task)

    assert decision.matched is False


@pytest.mark.parametrize(
    "task",
    [
        "Implement Command Center user interface state routing.",
        "Implement Command Center app-state table selection model.",
        "Implement Command Center data table normalization.",
        "Implement Command Center config table defaults.",
        "Implement Command Center chart query model.",
    ],
)
def test_non_visual_domain_table_chart_or_interface_work_does_not_route(task):
    decision = resolve_ui_work_route(_cfg(), task=task)

    assert decision.matched is False
    assert "non-visual domain keyword" in decision.advisory_reason


@pytest.mark.parametrize(
    "task",
    [
        "Implement Command Center card footer visual polish/responsive styling.",
        "Style Command Center data table rows for scanability.",
        "Style data table row spacing.",
        "Polish Command Center chart colors and labels.",
        "Polish chart colors.",
        "Implement responsive Command Center table layout.",
        "Add visual chart tooltip states.",
        "Add visual chart tooltip styling.",
        "Polish Command Center card colors.",
    ],
)
def test_visual_table_chart_and_card_work_selects_automatic_advisor(task):
    decision = resolve_ui_work_route(_cfg(), task=task)

    assert decision.matched is True
    assert decision.selected_route == "ui_visual_specialist"
    assert decision.advisory_matched is True
    assert "visual ui work" in decision.advisory_reason


def test_legacy_route_delegate_task_false_does_not_disable_automatic_advisor():
    cfg = _cfg()
    cfg["ui_work"]["route_delegate_task"] = False

    decision = resolve_ui_work_route(
        cfg,
        task="Implement responsive dashboard card visual polish.",
    )

    assert decision.matched is True
    assert decision.selected_route == "ui_visual_specialist"
    assert decision.advisory_matched is True


def test_pr_review_follow_up_around_page_layout_does_not_route():
    decision = resolve_ui_work_route(
        _cfg(),
        task="PR review follow-up for Command Center page layout comments.",
    )

    assert decision.matched is False
    assert "negative keyword" in decision.advisory_reason


def test_pid_project_name_alone_does_not_route():
    decision = resolve_ui_work_route(
        _cfg(),
        task="Repair scraper retry backoff for source ingestion.",
        cwd="/home/droid/workspaces/pid",
        project="PID",
    )

    assert decision.matched is False


def test_pid_dashboard_work_routes():
    decision = resolve_ui_work_route(
        _cfg(),
        task="Polish dashboard row spacing for PID source ledger.",
        cwd="/home/droid/workspaces/pid",
        project="PID",
        route_decision="ui_visual_specialist",
    )

    assert decision.matched is True
    assert "spacing" in decision.advisory_reason


def test_disabled_config_reports_match_without_overlay():
    cfg = _cfg()
    cfg["ui_work"]["enabled"] = False

    decision = resolve_ui_work_route(
        cfg,
        task="Polish frontend chart labels.",
        route_decision="ui_visual_specialist",
    )

    assert decision.matched is True
    assert decision.enabled is False
    assert decision.backend_config == {}
    assert decision.selected_route == "default_coding_worker"
    assert decision.fallback_used is True
    assert "ui_work.enabled" in decision.fallback_reason


def test_legacy_ui_runtime_config_is_ignored():
    cfg = _cfg()
    cfg["ui_work"].update(
        {
            "specialist_backend": "claude_code",
            "provider": "openrouter",
            "model": "anthropic/claude-fable-5",
            "route": "api_key",
            "reasoning_effort": "high",
            "fallback": {"allow_default_worker": False},
        }
    )

    decision = resolve_ui_work_route(
        cfg,
        task="Polish frontend chart labels.",
        backend="opencode",
        route_decision="ui_visual_specialist",
    )

    assert decision.matched is True
    assert decision.enabled is True
    assert decision.error == ""
    assert decision.selected_route == "ui_visual_specialist"
    assert decision.backend == "opencode"
    assert decision.provider == ""
    assert decision.model == ""
    assert decision.selected_provider == ""
    assert decision.selected_model == ""
    assert decision.backend_config == {}
    assert decision.fallback_allowed is False
    assert decision.fallback_used is False
