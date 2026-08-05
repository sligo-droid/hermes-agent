import json
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "sanitize_skills_index.py"


def test_sanitizer_filters_and_writes_destination_atomically(tmp_path):
    retired_name = "obsi" + "dian"
    source = tmp_path / "download.json"
    destination = tmp_path / "public" / "skills-index.json"
    source.write_text(json.dumps({"skills": [
        {"name": "notes", "identifier": "github/example/notes"},
        {"name": retired_name, "identifier": f"github/example/{retired_name}"},
    ]}))

    result = subprocess.run(
        [
            sys.executable, str(SCRIPT), "--input", str(source),
            "--output", str(destination),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    data = json.loads(destination.read_text())
    assert [entry["name"] for entry in data["skills"]] == ["notes"]
    assert data["skill_count"] == 1


def test_sanitizer_fails_closed_without_overwriting_destination(tmp_path):
    source = tmp_path / "invalid.json"
    destination = tmp_path / "skills-index.json"
    source.write_text('{"skills": "invalid"}')
    destination.write_text("preserve")

    result = subprocess.run(
        [
            sys.executable, str(SCRIPT), "--input", str(source),
            "--output", str(destination),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert destination.read_text() == "preserve"
