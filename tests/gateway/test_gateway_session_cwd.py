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


def test_project_channel_uses_workspace_cwd_but_keeps_project_context():
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="thread-1",
        chat_name="Sligo Labs / #pid / feature",
        chat_type="thread",
        parent_chat_id=PID_CHANNEL_ID,
        thread_id="thread-1",
        project_name="PID",
        project_path="/home/droid/.hermes/workspace/PID",
        project_channel_id=PID_CHANNEL_ID,
    )

    assert _resolve(source) == "/home/droid/workspaces"
    assert source.project_path == "/home/droid/.hermes/workspace/PID"


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
