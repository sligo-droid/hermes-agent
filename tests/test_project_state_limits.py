from pathlib import Path


PROJECT_STATE_ROUTING_CURSOR_MAX_BYTES = 3_000


def test_project_state_stays_under_routing_cursor_byte_limit():
    repo_root = Path(__file__).resolve().parents[1]
    project_state = repo_root / "docs" / "project-state.md"

    size = len(project_state.read_bytes())

    assert size <= PROJECT_STATE_ROUTING_CURSOR_MAX_BYTES, (
        f"docs/project-state.md is {size} bytes; routing cursor limit is "
        f"{PROJECT_STATE_ROUTING_CURSOR_MAX_BYTES} bytes. Keep only narrow "
        "routing/pickup facts here; move authoritative state to code, tests, "
        "config, PRs, docs/context.md, docs/decisions/, or focused runbooks."
    )
