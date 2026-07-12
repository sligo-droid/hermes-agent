"""Kanban role worker propagation for named model tiers."""

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
    assert settings["reasoning"] == "max"
    assert settings["reasoning_source"] == "model_tier"


def test_child_worker_applies_tier_to_opencode_and_codex(monkeypatch):
    from hermes_cli import kanban_codex_worker as worker

    monkeypatch.setenv("HERMES_CODEX_WORKER_MODEL_TIER", "worker")
    monkeypatch.setenv("HERMES_OPENCODE_WORKER_MODEL", "custom/dev-worker")
    monkeypatch.setenv("HERMES_CODEX_WORKER_MODEL", "custom/dev-model")
    monkeypatch.setenv("HERMES_CODEX_WORKER_REASONING", "max")
    monkeypatch.setenv("HERMES_CODEX_WORKER_SERVICE_TIER", "normal")

    assert worker._scheduled_opencode_worker_config() == {
        "opencode": {"model": "custom/dev-worker"},
        "simple_build_reasoning_level": "max",
        "complex_plan_reasoning_level": "max",
        "complex_build_reasoning_level": "max",
    }
    assert worker._role_extra_args("dev") == [
        "-c", 'model="custom/dev-model"',
        "-c", 'model_reasoning_effort="max"',
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
    assert captured["env"]["HERMES_CODEX_WORKER_MODEL"] == "custom/dev-model"
    assert captured["env"]["HERMES_OPENCODE_WORKER_MODEL"] == "custom/dev-worker"
