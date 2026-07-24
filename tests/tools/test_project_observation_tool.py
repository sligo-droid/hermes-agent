from __future__ import annotations

import copy
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import yaml

from agent.runtime_capabilities import ToolEffect
from model_tools import get_tool_definitions
from tools.project_observation_tool import (
    PROJECT_OBSERVE_SCHEMA,
    _OUTPUT_LIMIT,
    project_observe,
)
from tools.registry import registry


def _write_config(data: dict) -> None:
    home = Path(os.environ["HERMES_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")


def _write_script(
    project: Path,
    body: str,
    relative_path: str = "status.py",
) -> Path:
    project.mkdir(parents=True, exist_ok=True)
    script = project / relative_path
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(body, encoding="utf-8")
    return script


def test_configured_fixed_observation_executes_with_sanitized_env_and_option(
    tmp_path, monkeypatch
):
    project = tmp_path / "project"
    _write_script(
        project,
        """
import json
import os
import sys

print(json.dumps({
    "cwd": os.getcwd(),
    "repo": sys.argv[sys.argv.index("--repo") + 1],
    "python": sys.orig_argv[0],
    "human": "--human" in sys.argv,
    "secret": os.environ.get("PROJECT_OBSERVE_SECRET"),
}))
""".strip(),
        "scripts/local_lifecycle/runtime_status.py",
    )
    monkeypatch.setenv("PROJECT_OBSERVE_SECRET", "must-not-pass")
    _write_config(
        {
            "project_observations": {
                "runtime-status": {
                    "description": "Authoritative project status",
                    "cwd": str(project),
                    "argv": [
                        "python3",
                        "scripts/local_lifecycle/runtime_status.py",
                        "--repo",
                        str(project),
                    ],
                    "options": {
                        "human": {
                            "type": "boolean",
                            "default": False,
                            "true_argv": ["--human"],
                        }
                    },
                }
            }
        }
    )

    result = json.loads(
        registry.dispatch(
            "project_observe",
            {
                "operation": "run",
                "name": "runtime-status",
                "options": {"human": True},
            },
            runtime_mode="read_only",
        )
    )

    assert result["success"] is True
    assert result["exit_code"] == 0
    assert result["timed_out"] is False
    assert result["output_truncated"] is False
    assert result["options"] == {"human": True}
    assert result["parsed_output"] == {
        "cwd": str(project.resolve()),
        "repo": str(project),
        "python": "python3",
        "human": True,
        "secret": None,
    }


def test_observation_output_is_bounded(tmp_path):
    project = tmp_path / "project"
    _write_script(project, f"print('x' * {_OUTPUT_LIMIT + 20_000})")
    _write_config(
        {
            "project_observations": {
                "large-status": {
                    "cwd": str(project),
                    "argv": [sys.executable, "status.py"],
                }
            }
        }
    )

    result = json.loads(
        project_observe({"operation": "run", "name": "large-status"})
    )

    assert result["success"] is True
    assert result["output_truncated"] is True
    assert len(result["stdout"]) <= _OUTPUT_LIMIT
    assert "project observation output truncated" in result["stdout"]


def test_observation_timeout_kills_the_fixed_command(tmp_path):
    project = tmp_path / "project"
    _write_script(project, "import time; time.sleep(10)")
    _write_config(
        {
            "project_observations": {
                "slow-status": {
                    "cwd": str(project),
                    "argv": [sys.executable, "status.py"],
                    "timeout_seconds": 1,
                }
            }
        }
    )

    started = time.monotonic()
    result = json.loads(
        project_observe({"operation": "run", "name": "slow-status"})
    )

    assert time.monotonic() - started < 4
    assert result["success"] is False
    assert result["timed_out"] is True
    assert result["exit_code"] is None
    assert "1s timeout" in result["error"]


def test_model_cannot_override_argv_cwd_or_environment(tmp_path):
    project = tmp_path / "project"
    marker = tmp_path / "override-ran"
    _write_script(project, "print('configured')")
    _write_config(
        {
            "project_observations": {
                "status": {
                    "cwd": str(project),
                    "argv": [sys.executable, "status.py"],
                }
            }
        }
    )

    result = json.loads(
        project_observe(
            {
                "operation": "run",
                "name": "status",
                "argv": [sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"],
                "cwd": str(tmp_path),
                "env": {"SECRET": "value"},
            }
        )
    )

    assert "Unsupported project_observe arguments" in result["error"]
    assert not marker.exists()
    properties = PROJECT_OBSERVE_SCHEMA["parameters"]["properties"]
    assert set(properties) == {"operation", "name", "options"}
    assert PROJECT_OBSERVE_SCHEMA["parameters"]["additionalProperties"] is False


def test_unknown_and_unavailable_observations_fail_closed(tmp_path):
    project = tmp_path / "project"
    _write_script(project, "print('status')")
    _write_config(
        {
            "project_observations": {
                "missing": {
                    "cwd": str(tmp_path / "does-not-exist"),
                    "argv": [sys.executable, "status.py"],
                },
                "custom-env": {
                    "cwd": str(project),
                    "argv": [sys.executable, "status.py"],
                    "env": {"SECRET": "operator-injection-is-not-supported"},
                },
            }
        }
    )

    unknown = json.loads(
        project_observe({"operation": "run", "name": "not-configured"})
    )
    unavailable = json.loads(
        project_observe({"operation": "run", "name": "missing"})
    )
    custom_env = json.loads(
        project_observe({"operation": "run", "name": "custom-env"})
    )
    listed = json.loads(project_observe({"operation": "list"}))

    assert unknown["error"] == "Unknown project observation: not-configured"
    assert "cwd is unavailable" in unavailable["error"]
    assert "unsupported keys: env" in custom_env["error"]
    by_name = {item["name"]: item for item in listed["observations"]}
    assert by_name["missing"]["available"] is False
    assert "cwd is unavailable" in by_name["missing"]["unavailable_reason"]
    assert by_name["custom-env"]["available"] is False


def test_empty_config_has_harmless_list_and_clear_run_error():
    _write_config({})

    listed = json.loads(project_observe({"operation": "list"}))
    run = json.loads(project_observe({"operation": "run", "name": "pid"}))

    assert listed == {
        "success": True,
        "operation": "list",
        "observations": [],
        "error": None,
    }
    assert run["error"] == "Unknown project observation: pid"


def test_dynamic_schema_reflects_names_and_options_without_mutating_base(tmp_path):
    project = tmp_path / "project"
    _write_script(project, "print('{}')")
    base_schema = copy.deepcopy(PROJECT_OBSERVE_SCHEMA)
    _write_config(
        {
            "project_observations": {
                "pid-runtime-status": {
                    "description": "Authoritative PID status",
                    "cwd": str(project),
                    "argv": [sys.executable, "status.py"],
                    "options": {
                        "human": {
                            "type": "boolean",
                            "description": "Human-readable output",
                            "true_argv": ["--human"],
                        }
                    },
                },
                "worker-health": {
                    "description": "Worker health summary",
                    "cwd": str(project),
                    "argv": [sys.executable, "status.py"],
                },
            }
        }
    )

    definition = registry.get_definitions({"project_observe"})[0]["function"]

    assert definition["parameters"]["properties"]["name"]["enum"] == [
        "pid-runtime-status",
        "worker-health",
    ]
    assert definition["parameters"]["properties"]["options"]["properties"] == {
        "human": {
            "type": "boolean",
            "description": "Human-readable output",
            "default": False,
        }
    }
    assert "pid-runtime-status: Authoritative PID status" in definition["description"]
    assert PROJECT_OBSERVE_SCHEMA == base_schema


def test_read_only_and_action_modes_expose_same_observational_contract():
    _write_config({})
    with patch("tools.registry._check_fn_cached", return_value=True):
        read_only_defs = get_tool_definitions(
            enabled_toolsets=["terminal", "file"],
            quiet_mode=False,
            runtime_mode="read_only",
        )
        action_defs = get_tool_definitions(
            enabled_toolsets=["terminal", "file"],
            quiet_mode=False,
            runtime_mode="action",
        )

    read_only_names = {item["function"]["name"] for item in read_only_defs}
    action_names = {item["function"]["name"] for item in action_defs}
    entry = registry.get_entry("project_observe")

    assert "project_observe" in read_only_names
    assert "project_observe" in action_names
    assert {"write_file", "patch"}.isdisjoint(read_only_names)
    assert {"write_file", "patch"} <= action_names
    assert entry is not None and entry.effect is ToolEffect.READ_ONLY
    assert registry.read_only_block("project_observe", {"operation": "list"}) is None
    assert json.loads(
        registry.dispatch(
            "project_observe", {"operation": "list"}, runtime_mode="action"
        )
    )["success"] is True


def test_default_config_keeps_registry_empty_without_migration():
    from hermes_cli.config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["project_observations"] == {}
