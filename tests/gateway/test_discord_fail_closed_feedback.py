import logging
from unittest.mock import MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from plugins.platforms.discord.adapter import DiscordAdapter, interactive_setup


def _make_adapter() -> DiscordAdapter:
    return DiscordAdapter(PlatformConfig(enabled=True, token="***"))


def _make_runner(adapter: DiscordAdapter) -> GatewayRunner:
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner.adapters = {Platform.DISCORD: adapter}
    runner._profile_adapters = {}
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = False
    runner.pairing_stores = {}
    return runner


def _source(user_id: str, *, chat_id: str = "12345") -> SessionSource:
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id=chat_id,
        chat_type="channel",
        user_id=user_id,
    )


@pytest.fixture(autouse=True)
def _clear_discord_auth_env(monkeypatch):
    for var in (
        "DISCORD_ALLOWED_USERS",
        "DISCORD_ALLOWED_ROLES",
        "DISCORD_ALLOWED_CHANNELS",
        "DISCORD_ALLOW_ALL_USERS",
        "GATEWAY_ALLOWED_USERS",
        "GATEWAY_ALLOW_ALL_USERS",
    ):
        monkeypatch.delenv(var, raising=False)


def test_discord_open_default_logs_once(monkeypatch, caplog):
    adapter = _make_adapter()
    adapter._allowed_user_ids = set()
    adapter._allowed_role_ids = set()

    with caplog.at_level(logging.WARNING):
        assert adapter._is_allowed_user("42", is_dm=True) is True
        assert adapter._is_allowed_user("43", is_dm=True) is True

    messages = [record.message for record in caplog.records]
    matches = [
        msg for msg in messages
        if "trusted-development default allows messages" in msg
    ]
    assert len(matches) == 1
    assert "DISCORD_ALLOWED_USERS" in matches[0]
    assert "DISCORD_ALLOWED_CHANNELS" in matches[0]


def test_discord_open_default_warning_skips_explicit_channel_gate(monkeypatch, caplog):
    adapter = _make_adapter()
    adapter._allowed_user_ids = set()
    adapter._allowed_role_ids = set()
    monkeypatch.setenv("DISCORD_ALLOWED_CHANNELS", "12345")

    with caplog.at_level(logging.WARNING):
        assert (
            adapter._is_allowed_user(
                "42",
                guild=MagicMock(),
                is_dm=False,
                channel_ids={"12345"},
            )
            is True
        )

    assert "trusted-development default allows messages" not in caplog.text


def test_discord_setup_existing_token_explains_open_default(monkeypatch):
    info_lines: list[str] = []
    yes_no_answers = iter([False, False])

    def fake_get_env_value(key: str):
        return "token" if key == "DISCORD_BOT_TOKEN" else ""

    monkeypatch.setattr("hermes_cli.config.get_env_value", fake_get_env_value)
    monkeypatch.setattr("hermes_cli.config.save_env_value", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("hermes_cli.cli_output.print_header", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("hermes_cli.cli_output.print_success", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("hermes_cli.cli_output.print_info", lambda msg="", **_kwargs: info_lines.append(str(msg)))
    monkeypatch.setattr("hermes_cli.cli_output.prompt", lambda *_args, **_kwargs: "")
    monkeypatch.setattr("hermes_cli.cli_output.prompt_yes_no", lambda *_args, **_kwargs: next(yes_no_answers))

    interactive_setup()

    joined = "\n".join(info_lines)
    assert "trusted-development default" in joined
    assert "configure allowed users, roles, or channels" in joined


def test_discord_setup_new_token_empty_allowlist_explains_open_default(monkeypatch):
    info_lines: list[str] = []
    prompts = iter(["token", "", ""])

    monkeypatch.setattr("hermes_cli.config.get_env_value", lambda _key: "")
    monkeypatch.setattr("hermes_cli.config.save_env_value", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("hermes_cli.cli_output.print_header", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("hermes_cli.cli_output.print_success", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("hermes_cli.cli_output.print_info", lambda msg="", **_kwargs: info_lines.append(str(msg)))
    monkeypatch.setattr("hermes_cli.cli_output.prompt", lambda *_args, **_kwargs: next(prompts))
    monkeypatch.setattr("hermes_cli.cli_output.prompt_yes_no", lambda *_args, **_kwargs: False)

    interactive_setup()

    joined = "\n".join(info_lines)
    assert "Discord remains open for the trusted-development workflow" in joined
    assert "DISCORD_ALLOWED_ROLES" in joined
    assert "DISCORD_ALLOWED_CHANNELS" in joined


def test_discord_empty_policy_is_fail_closed_at_gateway_boundary():
    adapter = _make_adapter()
    adapter._allowed_user_ids = set()
    adapter._allowed_role_ids = set()
    runner = _make_runner(adapter)

    # The Discord adapter keeps its trusted-development intake default, but the
    # shared gateway authorization boundary must not treat arrival as proof of
    # authorization. Direct network adapters remain fail-closed unless an
    # allowlist, role/channel decision, pairing grant, or allow-all flag applies.
    assert adapter._is_allowed_user("42", is_dm=True) is True
    assert runner._is_user_authorized(_source("42")) is False


def test_discord_user_policy_remains_fail_closed_end_to_end(monkeypatch):
    monkeypatch.setenv("DISCORD_ALLOWED_USERS", "42")
    adapter = _make_adapter()
    adapter._allowed_user_ids = {"42"}
    adapter._allowed_role_ids = set()
    runner = _make_runner(adapter)

    assert adapter._is_allowed_user("42", is_dm=True) is True
    assert runner._is_user_authorized(_source("42")) is True
    assert adapter._is_allowed_user("99", is_dm=True) is False
    assert runner._is_user_authorized(_source("99")) is False


def test_discord_channel_policy_remains_fail_closed_end_to_end(monkeypatch):
    monkeypatch.setenv("DISCORD_ALLOWED_CHANNELS", "12345")
    adapter = _make_adapter()
    adapter._allowed_user_ids = set()
    adapter._allowed_role_ids = set()
    runner = _make_runner(adapter)
    guild = MagicMock()

    assert (
        adapter._is_allowed_user(
            "42", guild=guild, is_dm=False, channel_ids={"12345"}
        )
        is True
    )
    assert runner._is_user_authorized(_source("42", chat_id="12345")) is True
    assert (
        adapter._is_allowed_user(
            "42", guild=guild, is_dm=False, channel_ids={"99999"}
        )
        is False
    )
    assert runner._is_user_authorized(_source("42", chat_id="99999")) is False
