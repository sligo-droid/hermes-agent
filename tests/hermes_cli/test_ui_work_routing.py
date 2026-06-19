from __future__ import annotations

import copy

import pytest

from hermes_cli.config import DEFAULT_CONFIG
from hermes_cli.ui_work_routing import codex_ui_work_extra_args, resolve_ui_work_route


def _cfg():
    return copy.deepcopy(DEFAULT_CONFIG)


@pytest.mark.parametrize(
    "task",
    [
        "Implement Command Center card footer visuals and responsive styling.",
        "Polish Command Center card footer visuals and responsive styling.",
        "Redesign Command Center card footer visuals and responsive styling.",
    ],
)
def test_matches_new_visual_web_ui_development_and_overlay_args(task):
    decision = resolve_ui_work_route(
        _cfg(),
        task=task,
        backend="codex",
    )

    assert decision.matched is True
    assert decision.enabled is True
    assert "visual ui work" in decision.reason
    assert decision.provider == "openrouter"
    assert decision.model == "z-ai/glm-5.2"
    assert decision.backend_config["provider_config_key"] == "model_provider"
    assert codex_ui_work_extra_args(decision) == [
        "-c",
        'model_provider="openrouter"',
        "-c",
        'model="z-ai/glm-5.2"',
        "-c",
        'model_providers.openrouter.name="openrouter"',
        "-c",
        'model_providers.openrouter.base_url="https://openrouter.ai/api/v1"',
        "-c",
        'model_providers.openrouter.env_key="OPENROUTER_API_KEY"',
    ]


def test_tui_terminal_rendering_work_does_not_route():
    decision = resolve_ui_work_route(
        _cfg(),
        title="Hermes TUI terminal rendering follow-up",
        task="Fix TUI repaint clipping in the terminal session transcript.",
    )

    assert decision.matched is False
    assert "negative keyword" in decision.reason


def test_review_or_look_at_ui_does_not_route():
    decision = resolve_ui_work_route(
        _cfg(),
        task="Review the Command Center UI and look at the page layout for issues.",
    )

    assert decision.matched is False
    assert "negative keyword" in decision.reason


def test_negative_backend_only_overrides_ui_keyword():
    decision = resolve_ui_work_route(
        _cfg(),
        task="Backend-only API cleanup; no frontend or UI changes.",
        context="The word dashboard appears in an API response fixture.",
    )

    assert decision.matched is False
    assert "negative keyword" in decision.reason


def test_backend_api_work_with_ui_context_does_not_route():
    decision = resolve_ui_work_route(
        _cfg(),
        task="Implement backend API pagination for the application UI context.",
        context="The frontend page will consume this endpoint later.",
    )

    assert decision.matched is False
    assert "negative keyword" in decision.reason


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
    assert "non-visual domain keyword" in decision.reason


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
def test_visual_table_chart_and_card_work_routes_despite_domain_words(task):
    decision = resolve_ui_work_route(_cfg(), task=task)

    assert decision.matched is True
    assert "visual ui work" in decision.reason


def test_pr_review_follow_up_around_page_layout_does_not_route():
    decision = resolve_ui_work_route(
        _cfg(),
        task="PR review follow-up for Command Center page layout comments.",
    )

    assert decision.matched is False
    assert "negative keyword" in decision.reason


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
    )

    assert decision.matched is True
    assert "spacing" in decision.reason


def test_disabled_config_reports_match_without_overlay():
    cfg = _cfg()
    cfg["ui_work"]["enabled"] = False

    decision = resolve_ui_work_route(cfg, task="Polish frontend chart labels.")

    assert decision.matched is True
    assert decision.enabled is False
    assert decision.backend_config == {}
    assert codex_ui_work_extra_args(decision) == []


def test_missing_model_errors_when_fallback_disabled():
    cfg = _cfg()
    cfg["ui_work"]["model"] = ""
    cfg["ui_work"]["fallback"]["allow_default_worker"] = False

    decision = resolve_ui_work_route(cfg, task="Polish frontend chart labels.")

    assert decision.matched is True
    assert decision.error
    assert "provider and ui_work.model" in decision.error


def test_missing_model_fails_closed_even_when_fallback_allowed():
    cfg = _cfg()
    cfg["ui_work"]["model"] = ""
    cfg["ui_work"]["fallback"]["allow_default_worker"] = True

    decision = resolve_ui_work_route(cfg, task="Polish frontend chart labels.")

    assert decision.matched is True
    assert decision.fallback_allowed is True
    assert "ui_work.provider and ui_work.model" in decision.error
    assert decision.backend_config == {}
