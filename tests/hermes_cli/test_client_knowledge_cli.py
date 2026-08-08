from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

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
        "reviews",
        "notify-reviews-once",
        "adopt-review-message",
        "requeue-review-notification",
        "refresh-review-notification",
        "repair-review-details",
        "run-once",
        "gmail-poll-once",
        "notion-preflight",
    ):
        if action in {"show", "retry", "quarantine"}:
            suffix = ["0" * 32]
        elif action == "notion-preflight":
            suffix = ["--project", "pid"]
        elif action == "adopt-review-message":
            suffix = ["--review-id", "a" * 64, "--message-id", "123"]
        elif action in {
            "requeue-review-notification", "refresh-review-notification",
            "repair-review-details",
        }:
            suffix = ["--review-id", "a" * 64]
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


def test_run_once_dry_run_reports_enablement_without_loading_stages(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "client_knowledge": {
                "notion": {"enabled": True},
                "extraction": {"enabled": False},
                "interpretation": {"enabled": True},
                "assimilation": {"enabled": False},
                "review_notifications": {"enabled": True},
                "honcho_projection": "invalid",
            }
        },
    )
    blocked = {
        "plugins.client_knowledge_gbrain.notion_archive",
        "plugins.client_knowledge_gbrain.extraction",
        "plugins.client_knowledge_gbrain.interpretation",
        "plugins.client_knowledge_gbrain.assimilation",
        "plugins.client_knowledge_gbrain.review",
        "plugins.client_knowledge_gbrain.honcho_projection",
    }
    for name in blocked:
        sys.modules.pop(name, None)
    args = argparse.Namespace(
        client_knowledge_action="run-once",
        dry_run=True,
        db_path=str(tmp_path / "private" / "intake.db"),
    )

    assert client_knowledge_command(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "downstream_writes": False,
        "ledger_exists": False,
        "ledger_writes": False,
        "mode": "dry_run",
        "stage_enablement": {
            "assimilation": False,
            "extraction": False,
            "honcho_projection": False,
            "interpretation": True,
            "notion": True,
            "review_notifications": True,
        },
    }
    assert blocked.isdisjoint(sys.modules)
    assert not (tmp_path / "private" / "intake.db").exists()


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
    sys.modules.pop("plugins.client_knowledge_gbrain.review", None)
    manager.discover_and_load(force=True)

    assert "client-knowledge" in manager._cli_commands
    assert callable(manager._cli_commands["client-knowledge"]["setup_fn"])
    assert callable(manager._cli_commands["client-knowledge"]["handler_fn"])
    assert "plugins.client_knowledge_gbrain.review" not in sys.modules


def test_real_cli_dry_run_does_not_import_stages_or_create_ledger(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "plugins:\n  enabled:\n    - client-knowledge-gbrain\n",
        encoding="utf-8",
    )
    ledger = tmp_path / "private" / "intake.db"
    probe = (
        "import json,sys; "
        "from hermes_cli.main import main; "
        f"sys.argv=['hermes','client-knowledge','run-once','--dry-run','--db-path',{str(ledger)!r}]; "
        "code=main(); "
        "print(json.dumps({'exit_code':code,'downstream_modules_imported':sorted("
        "name for name in sys.modules if name.startswith('plugins.client_knowledge_gbrain.') "
        "and name.rsplit('.',1)[-1] in {'notion_archive','extraction','interpretation',"
        "'assimilation','review','honcho_projection'})}))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        env={**os.environ, "HERMES_HOME": str(home)},
        text=True,
        capture_output=True,
        check=True,
    )
    lines = [json.loads(line) for line in completed.stdout.splitlines() if line.startswith("{")]
    assert lines[-1] == {"downstream_modules_imported": [], "exit_code": None}
    assert lines[0]["downstream_writes"] is False
    assert lines[0]["ledger_writes"] is False
    assert not ledger.exists()
    assert not ledger.parent.exists()


def test_refresh_review_notification_archives_legacy_card_without_deciding(tmp_path, capsys):
    store = IntakeStore(tmp_path / "private" / "intake.db")
    artifact = _artifact()
    store.insert_artifact(artifact)
    now = time.time()
    review_id = "a" * 64
    with store._write() as conn:
        conn.execute(
            "INSERT INTO extractions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "x" * 64, artifact.artifact_id, artifact.content_sha256, "m" * 64,
                "ev1", "lv1", "rv1", "extracted", "storage", "object",
                "1" * 64, 1, 1, "{}", now,
            ),
        )
        conn.execute(
            "INSERT INTO interpretation_envelopes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "e" * 64, artifact.artifact_id, "pid", artifact.content_sha256,
                "x" * 64, "1" * 64, "ev1", "sv1", "pv1", "task",
                "storage", "object", "2" * 64, 1, now,
            ),
        )
        conn.execute(
            "INSERT INTO interpretations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "i" * 64, "e" * 64, artifact.artifact_id, "x" * 64,
                "sv1", "pv1", "storage", "object", "3" * 64, 1,
                "provider", "model", "provider", "model", "advanced", "route",
                1, 1, 2, 0, 0, now,
            ),
        )
        conn.execute(
            "INSERT INTO assimilation_proposals VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "b" * 64, artifact.artifact_id, "i" * 64, "av1", "sv1", "pv1",
                "policy", "pid", "d" * 64, "storage", "object", "4" * 64, 1,
                "provider", "model", "provider", "model", "advanced", "route",
                1, "finding_grounding_mismatch", "head", None, 0, now,
            ),
        )
        conn.execute(
            "INSERT INTO client_knowledge_reviews("
            "review_id, assimilation_id, artifact_id, project_key, proposal_sha256, "
            "assimilation_version, state, reason_code, notification_state, "
            "notification_content_sha256, notification_message_id, notification_guild_id, "
            "notification_channel_id, notification_role_id, notification_marker, "
            "created_at, updated_at) VALUES(?,?,?,?,?,?,'pending',?,'confirmed',?,?,?,?,?,?,?,?)",
            (
                review_id, "b" * 64, artifact.artifact_id, "pid", "d" * 64, "av1",
                "finding_grounding_mismatch", "5" * 64, "400", "100", "200", "300",
                "[ck-review:legacy]", now, now,
            ),
        )

    args = argparse.Namespace(
        client_knowledge_action="refresh-review-notification",
        review_id=review_id,
        db_path=str(store.path),
    )
    assert client_knowledge_command(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"refreshed": True, "review_id": review_id}
    review = store.get_review(review_id)
    assert review["state"] == "pending"
    assert review["notification_state"] == "pending"
    assert review["notification_message_id"] is None
    with store._connect() as conn:
        history = conn.execute(
            "SELECT message_id, marker FROM client_knowledge_review_notification_history "
            "WHERE review_id=?",
            (review_id,),
        ).fetchone()
    assert tuple(history) == ("400", "[ck-review:legacy]")
    assert client_knowledge_command(args) == 1
