"""Discord-only command visibility contracts."""

from hermes_cli.commands import (
    COMMANDS,
    gateway_help_lines,
    slack_native_slashes,
    slack_subcommand_map,
    telegram_bot_commands,
)


def test_removed_tier_commands_are_absent_from_all_command_surfaces():
    discord_help = "\n".join(gateway_help_lines("discord"))
    generic_help = "\n".join(gateway_help_lines())
    telegram_help = "\n".join(gateway_help_lines("telegram"))
    slack_help = "\n".join(gateway_help_lines("slack"))

    for command in ("dumb", "smart"):
        assert f"/{command}" not in discord_help
        assert f"/{command}" not in generic_help
        assert f"/{command}" not in telegram_help
        assert f"/{command}" not in slack_help
        assert f"/{command}" not in COMMANDS

    telegram_names = {name for name, _description in telegram_bot_commands()}
    slack_names = {name for name, _description, _hint in slack_native_slashes()}
    slack_mapping = slack_subcommand_map()
    assert {"dumb", "smart"}.isdisjoint(telegram_names)
    assert {"dumb", "smart"}.isdisjoint(slack_names)
    assert {"dumb", "smart"}.isdisjoint(slack_mapping)
