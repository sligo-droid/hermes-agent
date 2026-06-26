from pathlib import Path


PROJECT_STATE_MAX_BYTES = 10_000


def test_project_state_stays_under_hard_byte_limit():
    repo_root = Path(__file__).resolve().parents[1]
    project_state = repo_root / "docs" / "project-state.md"

    size = len(project_state.read_bytes())

    assert size <= PROJECT_STATE_MAX_BYTES, (
        f"docs/project-state.md is {size} bytes; hard limit is "
        f"{PROJECT_STATE_MAX_BYTES} bytes. Move completed or historical "
        "material to docs/archive/ and keep only active pickup state here."
    )
