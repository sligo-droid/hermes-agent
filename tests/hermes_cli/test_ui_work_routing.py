from __future__ import annotations

import copy

from hermes_cli.config import DEFAULT_CONFIG
from hermes_cli.ui_work_routing import codex_ui_work_extra_args, resolve_ui_work_route


def _cfg():
    return copy.deepcopy(DEFAULT_CONFIG)


def test_matches_frontend_dashboard_work():
    decision = resolve_ui_work_route(
        _cfg(),
        task="Implement the frontend dashboard filters and responsive layout.",
        backend="codex",
    )

    assert decision.matched is True
    assert decision.enabled is True
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


def test_matches_tui_work():
    decision = resolve_ui_work_route(_cfg(), title="Fix TUI repaint clipping")

    assert decision.matched is True
    assert "tui" in decision.reason


def test_negative_backend_only_overrides_ui_keyword():
    decision = resolve_ui_work_route(
        _cfg(),
        task="Backend-only API cleanup; no frontend or UI changes.",
        context="The word dashboard appears in an API response fixture.",
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
        task="Polish dashboard rows for PID source ledger.",
        cwd="/home/droid/workspaces/pid",
        project="PID",
    )

    assert decision.matched is True
    assert "dashboard" in decision.reason


def test_disabled_config_reports_match_without_overlay():
    cfg = _cfg()
    cfg["ui_work"]["enabled"] = False

    decision = resolve_ui_work_route(cfg, task="Fix frontend chart labels.")

    assert decision.matched is True
    assert decision.enabled is False
    assert decision.backend_config == {}
    assert codex_ui_work_extra_args(decision) == []


def test_missing_model_errors_when_fallback_disabled():
    cfg = _cfg()
    cfg["ui_work"]["model"] = ""
    cfg["ui_work"]["fallback"]["allow_default_worker"] = False

    decision = resolve_ui_work_route(cfg, task="Fix frontend chart labels.")

    assert decision.matched is True
    assert decision.error
    assert "provider and ui_work.model" in decision.error


def test_missing_model_fails_closed_even_when_fallback_allowed():
    cfg = _cfg()
    cfg["ui_work"]["model"] = ""
    cfg["ui_work"]["fallback"]["allow_default_worker"] = True

    decision = resolve_ui_work_route(cfg, task="Fix frontend chart labels.")

    assert decision.matched is True
    assert decision.fallback_allowed is True
    assert "ui_work.provider and ui_work.model" in decision.error
    assert decision.backend_config == {}
