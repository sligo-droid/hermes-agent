"""Kanban role worker propagation for named model tiers."""

import copy
from types import SimpleNamespace

import pytest


def test_role_tier_supplies_model_and_reasoning(monkeypatch):
    from hermes_cli import kanban_codex_workers as workers

    monkeypatch.delenv("HERMES_CODEX_WORKER_REASONING", raising=False)
    config = {
        "model_tiers": {
            "worker": {
                "model": "custom/dev-model",
                "opencode_model": "custom/dev-worker",
                "reasoning_effort": "max",
            }
        },
        "roles": {
            "dev": {
                "model_tier": "worker",
                "reasoning": "low",
                "service_tier": "normal",
            }
        },
    }

    settings = workers._role_runtime_settings("dev", config)

    assert settings["model_tier"] == "worker"
    assert settings["model"] == "custom/dev-model"
    assert settings["opencode_model"] == "custom/dev-worker"
    assert settings["reasoning"] == "high"
    assert settings["reasoning_source"] == "model_tier"
    assert settings["model_tier_source"] == "role"
    assert settings["fast_mode"] is False
    assert settings["service_tier"] == "normal"
    assert settings["service_tier_source"] == "explicit"
    assert "worker_tier" not in settings
    assert "worker_tier_source" not in settings


def test_reviewer_role_keeps_review_only_advanced_reasoning(monkeypatch):
    from hermes_cli import kanban_codex_workers as workers

    monkeypatch.delenv("HERMES_CODEX_WORKER_REASONING", raising=False)
    config = {
        "model_tiers": {
            "advanced": {
                "model": "custom/reviewer",
                "opencode_model": "custom/reviewer-worker",
                "reasoning_effort": "xhigh",
            }
        },
        "roles": {"reviewer": {"model_tier": "advanced"}},
    }

    settings = workers._role_runtime_settings("reviewer", config, {"title": "Review PR"})

    assert settings["model_tier"] == "advanced"
    assert settings["reasoning"] == "xhigh"


def test_default_role_tier_beats_stale_profile_and_environment_reasoning(monkeypatch):
    from hermes_cli import kanban_codex_workers as workers
    from hermes_cli.config import DEFAULT_CONFIG

    monkeypatch.setenv("HERMES_CODEX_WORKER_REASONING", "max")
    config = copy.deepcopy(DEFAULT_CONFIG["kanban"]["discord_worker"])
    config["model_tiers"] = copy.deepcopy(DEFAULT_CONFIG["model_tiers"])
    config["roles"]["dev"]["reasoning"] = "minimal"

    settings = workers._role_runtime_settings("dev", config)

    assert settings["model_tier"] == "intermediate"
    assert settings["model"] == "gpt-5.6-sol"
    assert settings["reasoning"] == "medium"
    assert settings["reasoning_source"] == "model_tier"


@pytest.mark.parametrize(("tier_name", "fast_mode", "service_tier"), [
    ("basic", True, "fast"),
    ("intermediate", False, "normal"),
])
def test_named_role_tier_deterministically_controls_service_tier(
    monkeypatch, tier_name, fast_mode, service_tier
):
    from hermes_cli import kanban_codex_workers as workers

    monkeypatch.setenv("HERMES_CODEX_WORKER_SERVICE_TIER", "fast" if not fast_mode else "normal")
    settings = workers._role_runtime_settings(
        "dev",
        {"roles": {"dev": {"model_tier": tier_name, "service_tier": "auto"}}},
    )

    assert settings["fast_mode"] is fast_mode
    assert settings["service_tier"] == service_tier
    assert settings["service_tier_source"] == "model_tier"


def test_child_worker_applies_tier_to_opencode_and_codex(monkeypatch):
    from agent import opencode_worker
    from hermes_cli import kanban_codex_worker as worker

    monkeypatch.setenv("HERMES_CODEX_WORKER_MODEL_TIER", "worker")
    monkeypatch.setenv("HERMES_OPENCODE_WORKER_MODEL", "custom/dev-worker")
    monkeypatch.setenv("HERMES_CODEX_WORKER_MODEL", "custom/dev-model")
    monkeypatch.setenv("HERMES_CODEX_WORKER_REASONING", "max")
    monkeypatch.setenv("HERMES_CODEX_WORKER_SERVICE_TIER", "normal")

    scheduled = worker._scheduled_opencode_worker_config()
    assert scheduled == {
        "model_tier": "worker",
        "service_tier": "normal",
        "opencode": {"model": "custom/dev-worker"},
        "simple_build_reasoning_level": "xhigh",
        "complex_plan_reasoning_level": "xhigh",
        "complex_build_reasoning_level": "xhigh",
    }
    profiles = opencode_worker.load_coding_worker_pass_profiles(
        {
            "model_tiers": {
                "worker": {
                    "model": "custom/dev-model",
                    "opencode_model": "custom/dev-worker",
                    "reasoning_effort": "max",
                }
            },
            "coding_worker": {
                "simple_build_model_tier": "intermediate",
                "complex_plan_model_tier": "advanced",
                "complex_build_model_tier": "intermediate",
            },
        },
        worker_config=scheduled,
    )
    assert {
        name: (profile["model_tier"], profile["model"], profile["reasoning_level"])
        for name, profile in profiles.items()
    } == {
        "simple_build": ("worker", "custom/dev-worker", "high"),
        "complex_plan": ("worker", "custom/dev-worker", "high"),
        "complex_build": ("worker", "custom/dev-worker", "high"),
    }
    assert worker._role_extra_args("dev") == [
        "-c", 'model="custom/dev-model"',
        "-c", 'model_reasoning_effort="high"',
        "-c", 'service_tier="normal"',
    ]


@pytest.mark.parametrize(
    ("tier_name", "service_tier", "expected_fast_mode"),
    [("basic", "normal", False), ("intermediate", "fast", True)],
)
def test_scheduled_opencode_preserves_resolved_service_tier(
    monkeypatch, tier_name, service_tier, expected_fast_mode
):
    from agent import opencode_worker
    from hermes_cli import kanban_codex_worker as worker

    monkeypatch.setenv("HERMES_CODEX_WORKER_MODEL_TIER", tier_name)
    monkeypatch.setenv("HERMES_CODEX_WORKER_MODEL_TIER_SOURCE", "role")
    monkeypatch.setenv("HERMES_CODEX_WORKER_SERVICE_TIER", service_tier)
    monkeypatch.setenv("HERMES_OPENCODE_WORKER_MODEL", "hermes-codex/gpt-5.5")
    monkeypatch.setenv("HERMES_CODEX_WORKER_REASONING", "medium")

    scheduled = worker._scheduled_opencode_worker_config()
    assert scheduled["service_tier"] == service_tier
    pass_config = opencode_worker.load_coding_worker_pass_config(
        {
            "model_tiers": {
                "basic": {
                    "model": "gpt-5.6-luna",
                    "opencode_model": "hermes-codex/gpt-5.6-luna",
                    "reasoning_effort": "max",
                    "fast_mode": True,
                },
                "intermediate": {
                    "model": "gpt-5.6-sol",
                    "opencode_model": "hermes-codex/gpt-5.6-sol",
                    "reasoning_effort": "low",
                    "fast_mode": False,
                },
            },
            "coding_worker": {},
        },
        worker_config=scheduled,
    )

    assert pass_config["simple_build_fast_mode"] is expected_fast_mode


@pytest.mark.parametrize(
    ("service_tier", "child_tier", "expected_fast_mode"),
    [("fast", "intermediate", True), ("normal", "basic", False)],
)
def test_scheduled_opencode_forwards_service_without_model_tier(
    monkeypatch, service_tier, child_tier, expected_fast_mode
):
    from agent import opencode_worker
    from hermes_cli import kanban_codex_worker as worker

    for name in (
        "HERMES_CODEX_WORKER_MODEL_TIER",
        "HERMES_OPENCODE_WORKER_MODEL",
        "HERMES_CODEX_WORKER_REASONING",
        "HERMES_CODEX_WORKER_REASONING_SOURCE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HERMES_CODEX_WORKER_SERVICE_TIER", service_tier)

    scheduled = worker._scheduled_opencode_worker_config()

    assert scheduled == {"service_tier": service_tier}
    pass_config = opencode_worker.load_coding_worker_pass_config(
        {
            "model_tiers": {
                "basic": {
                    "model": "gpt-5.6-luna",
                    "opencode_model": "hermes-codex/gpt-5.6-luna",
                    "reasoning_effort": "max",
                    "fast_mode": True,
                },
                "intermediate": {
                    "model": "gpt-5.6-sol",
                    "opencode_model": "hermes-codex/gpt-5.6-sol",
                    "reasoning_effort": "low",
                    "fast_mode": False,
                },
            },
            "coding_worker": {"model_tier": child_tier},
        },
        worker_config=scheduled,
    )

    assert pass_config["simple_build_fast_mode"] is expected_fast_mode


def test_bare_opencode_model_is_rejected_before_worker_profile_launch():
    from agent.opencode_worker import load_coding_worker_pass_profiles

    config = {
        "model_tiers": {
            "broken": {
                "provider": "anthropic",
                "model": "claude-sonnet-5",
                "opencode_model": "claude-sonnet-5",
                "reasoning_effort": "medium",
            }
        },
        "coding_worker": {"model_tier": "broken"},
    }

    with pytest.raises(ValueError, match="opencode_model in provider/model form"):
        load_coding_worker_pass_profiles(config)


def test_host_spawner_forwards_tier_models_to_the_child(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_workers as workers
    from hermes_cli import kanban_db

    captured = {}
    board_db = tmp_path / "kanban.db"
    task = SimpleNamespace(id="tier-task", assignee="dev")
    settings = {
        "reasoning": "max",
        "reasoning_source": "model_tier",
        "model_tier": "worker",
        "model_tier_source": "role",
        "model": "custom/dev-model",
        "opencode_model": "custom/dev-worker",
        "service_tier": "normal",
        "service_tier_source": "explicit",
        "mode": "normal",
    }

    monkeypatch.setattr(workers, "_worker_env", lambda: {})
    monkeypatch.setattr(kanban_db, "kanban_db_path", lambda board=None: board_db)
    monkeypatch.setattr(kanban_db, "workspaces_root", lambda board=None: tmp_path / "workspaces")
    monkeypatch.setattr(workers, "_github_cli_config_dir", lambda env: None)

    def fake_spawn(*args, **kwargs):
        captured["env"] = args[3]
        return 123

    monkeypatch.setattr(workers, "_spawn_logged_process", fake_spawn)

    assert workers._spawn_host_worker(
        task,
        str(tmp_path / "workspace"),
        cfg={},
        settings=settings,
        log_settings=settings,
        backend="opencode",
        board=None,
    ) == 123
    assert captured["env"]["HERMES_CODEX_WORKER_MODEL_TIER"] == "worker"
    assert captured["env"]["HERMES_CODEX_WORKER_MODEL_TIER_SOURCE"] == "role"
    assert "HERMES_CODEX_WORKER_TIER" not in captured["env"]
    assert "HERMES_CODEX_WORKER_TIER_SOURCE" not in captured["env"]
    assert captured["env"]["HERMES_CODEX_WORKER_MODEL"] == "custom/dev-model"
    assert captured["env"]["HERMES_OPENCODE_WORKER_MODEL"] == "custom/dev-worker"
