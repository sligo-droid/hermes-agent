"""Tests for Yuanbao owner slash-command detection."""

from gateway.platforms.yuanbao import OwnerCommandMiddleware


def _detect(text: str, *, from_account: str = "owner"):
    return OwnerCommandMiddleware._detect_owner_command(
        push={"bot_owner_id": "owner"},
        msg_body=[{"msg_type": "TIMTextElem", "msg_content": {"text": text}}],
        chat_type="group",
        from_account=from_account,
    )


def test_owner_operational_commands_bypass_mention_gate():
    for command in (
        "/steer",
        "/goal",
        "/subgoal",
        "/status",
        "/restart",
        "/platform",
        "/sethome",
        "/set-home",
    ):
        cmd, cmd_line, is_owner = _detect(f"{command} arg")
        assert cmd == command
        assert cmd_line == f"{command} arg"
        assert is_owner is True


def test_non_owner_operational_command_is_detected_for_rejection():
    cmd, cmd_line, is_owner = _detect("/status", from_account="someone-else")
    assert cmd == "/status"
    assert cmd_line == "/status"
    assert is_owner is False


def test_unlisted_command_still_requires_normal_mention_gate():
    assert _detect("/model") == (None, None, False)
