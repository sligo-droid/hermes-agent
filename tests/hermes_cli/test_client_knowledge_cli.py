from __future__ import annotations

import argparse
import json

from plugins.client_knowledge_gbrain.cli import client_knowledge_command, register_cli
from plugins.client_knowledge_gbrain.models import IntakeArtifact
from plugins.client_knowledge_gbrain.store import IntakeStore


def _artifact() -> IntakeArtifact:
    return IntakeArtifact.from_bytes(
        project_key="pid",
        provider_id="gmail",
        provider_artifact_id="message-1",
        content=b"private body",
        received_at=10,
    )


def test_cli_parser_registers_all_operator_actions():
    parser = argparse.ArgumentParser()
    register_cli(parser)

    for action in (
        "status",
        "list",
        "show",
        "retry",
        "quarantine",
        "reconcile",
        "run-once",
        "gmail-poll-once",
        "notion-preflight",
    ):
        if action in {"show", "retry", "quarantine"}:
            suffix = ["0" * 32]
        elif action == "notion-preflight":
            suffix = ["--project", "pid"]
        else:
            suffix = []
        args = parser.parse_args([action] + suffix)
        assert args.client_knowledge_action == action
        assert args.func is client_knowledge_command


def test_cli_output_is_redacted_and_run_once_is_explicit(tmp_path, capsys):
    store = IntakeStore(tmp_path / "private" / "intake.db")
    artifact = _artifact()
    store.insert_artifact(artifact)
    store.add_job(artifact.artifact_id, "quarantined")

    args = argparse.Namespace(
        client_knowledge_action="list",
        status="",
        limit=20,
        db_path=str(store.path),
    )
    assert client_knowledge_command(args) == 0
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert artifact.content_sha256 not in output
    assert "spool_key" not in output
    assert "gmail" not in output
    assert "private body" not in output
    assert payload["jobs"][0]["stage"] == "quarantined"

    args.client_knowledge_action = "run-once"
    # Disabled-by-default Notion configuration remains a bounded no-work run.
    assert client_knowledge_command(args) == 0
    assert "notion_archive" in capsys.readouterr().out


def test_cli_rejects_noncanonical_job_ids_without_echoing_them(tmp_path, capsys):
    args = argparse.Namespace(
        client_knowledge_action="show",
        job_id="secret/path/customer.pdf",
        db_path=str(tmp_path / "private" / "intake.db"),
    )

    assert client_knowledge_command(args) == 1
    output = capsys.readouterr().out
    assert json.loads(output) == {"error_class": "ValueError"}
    assert "secret" not in output


def test_real_plugin_cli_discovery_registers_command(monkeypatch, tmp_path):
    from hermes_cli.plugins import PluginManager

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "plugins:\n  enabled:\n    - client-knowledge-gbrain\n",
        encoding="utf-8",
    )
    manager = PluginManager()
    manager.discover_and_load(force=True)

    assert "client-knowledge" in manager._cli_commands
    assert callable(manager._cli_commands["client-knowledge"]["setup_fn"])
    assert callable(manager._cli_commands["client-knowledge"]["handler_fn"])
