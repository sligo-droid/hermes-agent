import copy
from unittest.mock import patch

import yaml

from hermes_cli.config import (
    DEFAULT_CONFIG,
    HIGH_RISK_CONFIG_PATHS,
    build_high_risk_config_drift_report,
    config_command,
    load_config,
)


REQUIRED_RISK_CLASSES = {
    "security/autonomy",
    "worker execution",
    "scheduling/timeout",
    "memory/compression",
    "local path behavior",
}


def _set_path(config, dotted_path, value):
    node = config
    parts = dotted_path.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def _risk_items(report):
    return {
        item["path"]: item
        for items in report["groups"].values()
        for item in items
    }


def test_high_risk_registry_uses_required_acceptance_taxonomy():
    assert {spec.risk_class for spec in HIGH_RISK_CONFIG_PATHS} == REQUIRED_RISK_CLASSES

    risk_classes_by_path = {spec.path: spec.risk_class for spec in HIGH_RISK_CONFIG_PATHS}
    assert risk_classes_by_path == {
        "coding_worker.opencode.dangerously_skip_permissions": "security/autonomy",
        "coding_worker.opencode.startup_timeout_seconds": "scheduling/timeout",
        "coding_worker.simple_build_reasoning_level": "worker execution",
        "delegation.child_timeout_seconds": "scheduling/timeout",
        "delegation.max_concurrent_children": "worker execution",
        "delegation.subagent_auto_approve": "security/autonomy",
        "compression.protect_last_n": "memory/compression",
        "terminal.cwd": "local path behavior",
        "kanban.dispatch_stale_timeout_seconds": "scheduling/timeout",
    }


def test_high_risk_drift_groups_required_paths_and_sources(tmp_path):
    active = copy.deepcopy(DEFAULT_CONFIG)
    raw = {
        "delegation": {"max_concurrent_children": 7},
        "terminal": {"cwd": "/srv/sligo"},
    }
    _set_path(active, "delegation.max_concurrent_children", 7)
    _set_path(active, "terminal.cwd", "/srv/sligo")

    with patch.dict("os.environ", {"HERMES_HOME": str(tmp_path)}):
        report = build_high_risk_config_drift_report(active_config=active, raw_config=raw)

    assert set(report["groups"]) == {"worker execution", "local path behavior"}
    items = _risk_items(report)
    assert items["delegation.max_concurrent_children"]["active"] == 7
    assert items["delegation.max_concurrent_children"]["default"] == 3
    assert items["delegation.max_concurrent_children"]["source"] == str(tmp_path / "config.yaml")
    assert items["terminal.cwd"]["rationale"] == "unclassified"


def test_rationale_marks_known_intentional_but_keeps_drift_visible(tmp_path):
    active = copy.deepcopy(DEFAULT_CONFIG)
    raw = {"delegation": {"subagent_auto_approve": True}}
    _set_path(active, "delegation.subagent_auto_approve", True)
    rationale_path = tmp_path / "config-drift-rationales.yaml"
    rationale_path.write_text(
        yaml.safe_dump(
            {
                "rationales": {
                    "delegation.subagent_auto_approve": {
                        "rationale": "Local CI worker profile is isolated and monitored."
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    with patch.dict("os.environ", {"HERMES_HOME": str(tmp_path)}):
        report = build_high_risk_config_drift_report(
            active_config=active,
            raw_config=raw,
            rationale_path=rationale_path,
        )

    item = _risk_items(report)["delegation.subagent_auto_approve"]
    assert item["known_intentional"] is True
    assert item["rationale"] == "Local CI worker profile is isolated and monitored."
    assert item["active"] is True
    assert item["path"] == "delegation.subagent_auto_approve"


def test_missing_rationale_is_unclassified(tmp_path):
    active = copy.deepcopy(DEFAULT_CONFIG)
    raw = {"compression": {"protect_last_n": 5}}
    _set_path(active, "compression.protect_last_n", 5)

    with patch.dict("os.environ", {"HERMES_HOME": str(tmp_path)}):
        report = build_high_risk_config_drift_report(active_config=active, raw_config=raw)

    item = _risk_items(report)["compression.protect_last_n"]
    assert item["known_intentional"] is False
    assert item["rationale"] == "unclassified"


def test_secret_looking_and_env_sourced_values_are_redacted(tmp_path):
    active = copy.deepcopy(DEFAULT_CONFIG)
    defaults = copy.deepcopy(DEFAULT_CONFIG)
    _set_path(active, "terminal.cwd", "/expanded/private/workdir")
    _set_path(active, "example.api_key", "live-secret")
    _set_path(defaults, "example.api_key", "default-secret")
    raw = {
        "terminal": {"cwd": "${PRIVATE_WORKDIR}"},
        "example": {"api_key": "literal-secret"},
    }

    with patch.dict("os.environ", {"HERMES_HOME": str(tmp_path)}):
        report = build_high_risk_config_drift_report(
            active_config=active,
            default_config=defaults,
            raw_config=raw,
        )

    terminal_item = _risk_items(report)["terminal.cwd"]
    assert terminal_item["active"] == "<redacted>"
    assert terminal_item["default"] == "."
    assert "${VAR} reference" in terminal_item["source"]

    from hermes_cli.config import _redact_config_drift_value

    assert _redact_config_drift_value("example.api_key", "live-secret") == "<redacted>"


def test_config_audit_risk_cli_uses_effective_config_and_does_not_mutate(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump({"delegation": {"max_concurrent_children": 5}}),
        encoding="utf-8",
    )
    before = config_path.read_text(encoding="utf-8")

    class Args:
        config_command = "audit"
        risk = True

    with patch.dict("os.environ", {"HERMES_HOME": str(tmp_path)}):
        config_command(Args())
        loaded = load_config()

    output = capsys.readouterr().out
    assert "High-risk config drift audit" in output
    assert "delegation.max_concurrent_children" in output
    assert "worker execution" in output
    assert "read-only" in output
    assert loaded["delegation"]["max_concurrent_children"] == 5
    assert config_path.read_text(encoding="utf-8") == before


def test_config_audit_risk_output_redacts_env_sourced_value(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump({"terminal": {"cwd": "${PRIVATE_WORKDIR}"}}),
        encoding="utf-8",
    )

    class Args:
        config_command = "audit"
        risk = True

    with patch.dict("os.environ", {"HERMES_HOME": str(tmp_path), "PRIVATE_WORKDIR": "/private/workdir"}):
        config_command(Args())

    output = capsys.readouterr().out
    assert "<redacted>" in output
    assert "/private/workdir" not in output
    assert "${PRIVATE_WORKDIR}" not in output
