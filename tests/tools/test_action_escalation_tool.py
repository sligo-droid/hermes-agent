import json

from gateway.session_context import clear_session_vars, set_session_vars
from tools.action_escalation_tool import escalate_to_action


def test_action_escalation_requires_eligible_discord_intake():
    tokens = set_session_vars(platform="discord")
    try:
        result = json.loads(escalate_to_action(reason="needs implementation"))
    finally:
        clear_session_vars(tokens)

    assert result["success"] is False
    assert "question/intake" in result["error"]


def test_action_escalation_returns_side_effect_free_handoff_marker():
    tokens = set_session_vars(
        platform="discord",
        discord_action_escalation_allowed="1",
    )
    try:
        result = json.loads(escalate_to_action(reason="needs implementation"))
    finally:
        clear_session_vars(tokens)

    assert result == {
        "success": True,
        "action_escalation_requested": True,
        "reason": "needs implementation",
    }


def test_action_escalation_schema_is_available_only_on_discord_bundle():
    from model_tools import get_tool_definitions

    discord_names = {
        item["function"]["name"]
        for item in get_tool_definitions(
            enabled_toolsets=["hermes-discord", "discord-action-escalation"],
            quiet_mode=True,
        )
    }
    telegram_names = {
        item["function"]["name"]
        for item in get_tool_definitions(
            enabled_toolsets=["hermes-telegram"], quiet_mode=True
        )
    }

    assert "escalate_to_action" in discord_names
    assert "escalate_to_action" not in telegram_names
