import json
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "archive_legacy_vault.py"


def test_dry_run_does_not_mutate_source(tmp_path):
    source = tmp_path / "vault"
    source.mkdir()
    (source / "note.md").write_text("hello")
    archive = tmp_path / "archive"

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--source", str(source), "--archive-root", str(archive)],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert json.loads(result.stdout)["mode"] == "dry-run"
    assert source.exists()
    assert not archive.exists()


def test_execute_archives_verifies_and_removes_source(tmp_path):
    source = tmp_path / "vault"
    source.mkdir()
    (source / "note.md").write_text("hello")
    archive = tmp_path / "archive"

    result = subprocess.run(
        [
            sys.executable, str(SCRIPT), "--source", str(source),
            "--archive-root", str(archive), "--execute",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert not source.exists()
    destinations = list(archive.iterdir())
    assert len(destinations) == 1
    assert (destinations[0] / "note.md").read_text() == "hello"
    assert (destinations[0] / ".hermes-archive-manifest.json").exists()
