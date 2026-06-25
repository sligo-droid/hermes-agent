from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_board_health as health
from hermes_cli import kanban_db as kb


@pytest.fixture
def fresh_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for var in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_BOARD",
        kb._KANBAN_DB_HANDOFF_MARKER_ENV,
    ):
        monkeypatch.delenv(var, raising=False)
    try:
        import hermes_constants

        hermes_constants._cached_default_hermes_root = None  # type: ignore[attr-defined]
    except Exception:
        pass
    kb._INITIALIZED_PATHS.clear()
    return home


def _kanban_cli(*argv: str) -> int:
    root = argparse.ArgumentParser()
    subp = root.add_subparsers(dest="cmd")
    kanban_cli.build_parser(subp)
    ns = root.parse_args(["kanban", *argv])
    return kanban_cli.kanban_command(ns)


def _by_slug(rows: list[dict]) -> dict[str, dict]:
    return {row["slug"]: row for row in rows}


def test_scan_missing_default_db_does_not_create_file(fresh_home):
    db_path = kb.kanban_db_path(kb.DEFAULT_BOARD)
    assert not db_path.exists()

    rows = _by_slug(health.scan_boards())

    assert not db_path.exists()
    assert rows["default"]["exists"] is False
    assert rows["default"]["sqlite_header_status"] == "missing"
    assert rows["default"]["integrity_status"] is None


def test_scan_named_metadata_board_with_missing_db_does_not_create_file(fresh_home):
    board_dir = kb.board_dir("project")
    board_dir.mkdir(parents=True)
    (board_dir / "board.json").write_text('{"name":"Project"}', encoding="utf-8")
    db_path = kb.kanban_db_path("project")

    rows = _by_slug(health.scan_boards())

    assert not db_path.exists()
    assert rows["project"]["exists"] is False
    assert rows["project"]["sqlite_header_status"] == "missing"


def test_zero_byte_db_reports_stub_and_is_not_initialized(fresh_home):
    db_path = kb.kanban_db_path(kb.DEFAULT_BOARD)
    db_path.write_bytes(b"")

    row = _by_slug(health.scan_boards())["default"]

    assert db_path.exists()
    assert db_path.stat().st_size == 0
    assert row["exists"] is True
    assert row["zero_byte_stub"] is True
    assert row["sqlite_header_status"] == "zero_byte"
    assert row["integrity_status"] is None


def test_invalid_header_is_classified_without_corrupt_backup_or_incident(fresh_home):
    board_dir = kb.board_dir("bad")
    board_dir.mkdir(parents=True)
    metadata_path = board_dir / "board.json"
    original_metadata = '{"name":"Bad"}'
    metadata_path.write_text(original_metadata, encoding="utf-8")
    db_path = kb.kanban_db_path("bad")
    db_path.write_bytes(b"SQLit" + bytes([0x16, 0x03, 0x03, 0x00, 0x2A]) + b"not sqlite")

    row = _by_slug(health.scan_boards())["bad"]

    assert row["sqlite_header_status"] == "invalid"
    assert "TLS record header" in row["sqlite_header_reason"]
    assert row["integrity_status"] is None
    assert list(board_dir.glob("kanban.db.corrupt.*.bak")) == []
    assert metadata_path.read_text(encoding="utf-8") == original_metadata


def test_sidecars_and_corrupt_backups_are_reported_without_deletion(fresh_home):
    board_dir = kb.board_dir("sidecar")
    board_dir.mkdir(parents=True)
    (board_dir / "board.json").write_text("{}", encoding="utf-8")
    db_path = kb.kanban_db_path("sidecar")
    db_path.write_bytes(b"bad")
    wal_path = board_dir / "kanban.db-wal"
    shm_path = board_dir / "kanban.db-shm"
    backup_old = board_dir / "kanban.db.corrupt.1111111111.aaaa.bak"
    backup_new = board_dir / "kanban.db.corrupt.2222222222.bbbb.bak"
    wal_path.write_bytes(b"wal")
    shm_path.write_bytes(b"shm!")
    backup_old.write_bytes(b"old")
    backup_new.write_bytes(b"new")
    os.utime(backup_old, (100, 100))
    os.utime(backup_new, (200, 200))

    row = _by_slug(health.scan_boards())["sidecar"]

    assert row["wal_present"] is True
    assert row["wal_size"] == 3
    assert row["shm_present"] is True
    assert row["shm_size"] == 4
    assert row["corrupt_backup_count"] == 2
    assert row["historical_corrupt_backup_count"] == 2
    assert row["historical_artifact_count"] == 2
    assert row["historical_artifacts_status"] == "cleanup_noise"
    assert row["latest_corrupt_backup_mtime"] == 200
    assert wal_path.exists()
    assert shm_path.exists()
    assert backup_old.exists()
    assert backup_new.exists()


def test_cli_board_health_labels_corrupt_backups_as_historical_noise(fresh_home, capsys):
    board_dir = kb.board_dir("healthy-with-history")
    board_dir.mkdir(parents=True)
    (board_dir / "board.json").write_text("{}", encoding="utf-8")
    kb.init_db(board="healthy-with-history")
    backup = board_dir / "kanban.db.corrupt.2222222222.bbbb.bak"
    backup.write_bytes(b"old corrupt db")

    rc = _kanban_cli("diagnostics", "--board-health")
    output = capsys.readouterr().out

    assert rc == 0
    assert "integrity=ok" in output
    assert "historical cleanup artifacts: corrupt_backups=1" in output
    assert "not live board-health failures" in output


def test_healthy_initialized_db_reports_valid_header_and_ok_integrity(fresh_home):
    kb.init_db()

    row = _by_slug(health.scan_boards())["default"]

    assert row["exists"] is True
    assert row["size"] > 0
    assert row["zero_byte_stub"] is False
    assert row["sqlite_header_status"] == "valid"
    assert row["integrity_status"].lower() == "ok"


def test_cli_board_health_json_does_not_initialize_missing_default(fresh_home, capsys):
    db_path = kb.kanban_db_path(kb.DEFAULT_BOARD)

    rc = _kanban_cli("diagnostics", "--board-health", "--json")

    payload = json.loads(capsys.readouterr().out)
    rows = _by_slug(payload)
    assert rc == 0
    assert not db_path.exists()
    assert rows["default"]["sqlite_header_status"] == "missing"
