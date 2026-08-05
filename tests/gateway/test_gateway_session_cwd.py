from gateway.config import Platform
from gateway.session import SessionSource


DEV_CHANNEL_ID = "1504252294495998043"
PID_CHANNEL_ID = "1505275259006484570"


def _config():
    return {
        "terminal": {"cwd": "/home/droid/.hermes"},
        "discord": {
            "project_channel_cwd": "/home/droid/workspaces",
            "channel_cwds": {DEV_CHANNEL_ID: "/home/droid/hermes"},
        },
    }


def _resolve(source):
    # Import after the per-test HERMES_HOME fixture has run. Importing
    # gateway.run at collection time can cache the developer's live config.
    from gateway.run import _resolve_gateway_session_cwd

    return _resolve_gateway_session_cwd(source, _config())


def test_dev_channel_uses_hermes_repo_cwd():
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id=DEV_CHANNEL_ID,
        chat_name="Sligo Labs / #dev",
        chat_type="group",
    )

    assert _resolve(source) == "/home/droid/hermes"


def test_dev_thread_inherits_parent_channel_cwd():
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="thread-1",
        chat_name="Sligo Labs / #dev / feature",
        chat_type="thread",
        parent_chat_id=DEV_CHANNEL_ID,
        thread_id="thread-1",
    )

    assert _resolve(source) == "/home/droid/hermes"


def test_project_channel_uses_mapped_project_cwd_and_loads_agents(tmp_path):
    project = tmp_path / "PID"
    project.mkdir()
    (project / "AGENTS.md").write_text("PID feature scope rules.", encoding="utf-8")
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="thread-1",
        chat_name="Sligo Labs / #pid / feature",
        chat_type="thread",
        parent_chat_id=PID_CHANNEL_ID,
        thread_id="thread-1",
        project_name="PID",
        project_path=str(project),
        project_channel_id=PID_CHANNEL_ID,
    )

    cwd = _resolve(source)

    assert cwd == str(project)
    from agent.prompt_builder import build_context_files_prompt

    assert "PID feature scope rules." in build_context_files_prompt(
        cwd=cwd,
        skip_soul=True,
    )


def test_project_channel_with_missing_mapped_path_uses_project_fallback(tmp_path):
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id=PID_CHANNEL_ID,
        chat_name="Sligo Labs / #pid",
        chat_type="group",
        project_name="PID",
        project_path=str(tmp_path / "missing"),
        project_channel_id=PID_CHANNEL_ID,
    )

    assert _resolve(source) == "/home/droid/workspaces"


def test_unmapped_discord_channel_uses_default_gateway_cwd():
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="other-channel",
        chat_name="Sligo Labs / #general",
        chat_type="group",
    )

    assert _resolve(source) == "/home/droid/.hermes"


def test_non_discord_uses_default_gateway_cwd():
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="chat-1",
        chat_name="Group",
        chat_type="group",
    )

    assert _resolve(source) == "/home/droid/.hermes"
