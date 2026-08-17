"""Gateway control tool for promoting a Discord intake turn to action work."""

import json

from gateway.session_context import get_session_env
from tools.registry import registry


ACTION_ESCALATION_SCHEMA = {
    "name": "escalate_to_action",
    "description": (
        "Escalate the current Discord question/intake turn into Hermes' action-work "
        "runtime. Use only when the user is actually asking Hermes to implement, "
        "change, fix, deploy, run, or otherwise perform work. This is a control-plane "
        "handoff: after calling it, stop immediately. Do not inspect files, run commands, "
        "edit anything, or continue answering in the current turn. The gateway will "
        "replay the original user request in the proper action thread with its mutable "
        "worktree, action model tier, lifecycle summary, and verification policy."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": (
                    "Optional short reason the request requires action mode. This is "
                    "diagnostic only; the gateway always replays the original user request."
                ),
                "maxLength": 300,
            },
        },
        "required": [],
    },
}


def escalate_to_action(*, reason: str = "") -> str:
    """Return an auditable handoff marker without performing side effects."""
    platform = str(get_session_env("HERMES_SESSION_PLATFORM", "") or "").strip().lower()
    allowed = str(
        get_session_env("HERMES_DISCORD_ACTION_ESCALATION_ALLOWED", "") or ""
    ).strip()
    if platform != "discord" or allowed != "1":
        return json.dumps(
            {
                "success": False,
                "error": (
                    "Action escalation is available only during a Discord "
                    "question/intake turn."
                ),
            },
            ensure_ascii=False,
        )
    return json.dumps(
        {
            "success": True,
            "action_escalation_requested": True,
            "reason": str(reason or "").strip()[:300],
        },
        ensure_ascii=False,
    )


registry.register(
    name="escalate_to_action",
    toolset="discord-control",
    schema=ACTION_ESCALATION_SCHEMA,
    handler=lambda args, **_kw: escalate_to_action(reason=args.get("reason", "")),
    emoji="↗️",
    effect="read_only",
    runtime_modes={"read_only"},
)
