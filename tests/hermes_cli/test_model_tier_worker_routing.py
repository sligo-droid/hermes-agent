"""Kanban role worker propagation for named model tiers."""

import copy
from types import SimpleNamespace


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
        "coding_worker": {
            "worker_tiers": {
                "quick": {
                    "model": "custom/quick-model",
                    "opencode_model": "custom/quick-worker",
                    "reasoning_effort": "low",
                }
            }
        },
        "roles": {
            "dev": {
                "model_tier": "worker",
                "worker_tier": "quick",
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
    assert settings["worker_tier"] == "quick"
    assert settings["worker_tier_source"] == "role"


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
        "opencode": {"model": "custom/dev-worker"},
        "simple_build_reasoning_level": "max",
        "complex_plan_reasoning_level": "max",
        "complex_build_reasoning_level": "max",
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
        "worker_tier": "quick",
        "worker_tier_source": "role",
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
    assert captured["env"]["HERMES_CODEX_WORKER_TIER"] == "quick"
    assert captured["env"]["HERMES_CODEX_WORKER_TIER_SOURCE"] == "role"
    assert captured["env"]["HERMES_CODEX_WORKER_MODEL"] == "custom/dev-model"
    assert captured["env"]["HERMES_OPENCODE_WORKER_MODEL"] == "custom/dev-worker"
