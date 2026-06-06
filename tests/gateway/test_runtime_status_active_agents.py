from datetime import datetime

import pytest

from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.status import read_runtime_status
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@pytest.mark.asyncio
async def test_active_message_turn_refreshes_runtime_status(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    stale_updated_at = "2000-01-01T00:00:00+00:00"
    (tmp_path / "gateway_state.json").write_text(
        '{"gateway_state":"running","restart_requested":false,'
        '"active_agents":0,"updated_at":"' + stale_updated_at + '",'
        '"platforms":{}}',
        encoding="utf-8",
    )

    runner, _adapter = make_restart_runner()
    runner._update_runtime_status = GatewayRunner._update_runtime_status.__get__(
        runner, GatewayRunner
    )
    runner._refresh_active_agent_runtime_status = (
        GatewayRunner._refresh_active_agent_runtime_status.__get__(runner, GatewayRunner)
    )
    runner._release_running_agent_state = GatewayRunner._release_running_agent_state.__get__(
        runner, GatewayRunner
    )
    runner._session_run_generation = {}
    runner._busy_ack_ts = {}
    runner._accept_discord_work_item = lambda _event, _session_key: None
    runner._is_telegram_topic_root_lobby = lambda _source: False
    runner.hooks.emit_collect.return_value = []

    observed_active_status = None

    async def _handle_with_agent(_event, _source, _quick_key, _run_generation):
        nonlocal observed_active_status
        observed_active_status = read_runtime_status()
        return "ok"

    runner._handle_message_with_agent = _handle_with_agent

    event = MessageEvent(
        text="hello",
        message_type=MessageType.TEXT,
        source=make_restart_source(),
        message_id="m-active",
    )

    result = await runner._handle_message(event)

    assert result == "ok"
    assert observed_active_status is not None
    assert observed_active_status["gateway_state"] == "running"
    assert observed_active_status["active_agents"] == 1
    assert observed_active_status["restart_requested"] is False
    assert _parse_iso(observed_active_status["updated_at"]) > _parse_iso(stale_updated_at)

    final_status = read_runtime_status()
    assert final_status is not None
    assert final_status["gateway_state"] == "running"
    assert final_status["active_agents"] == 0
    assert final_status["restart_requested"] is False
    assert _parse_iso(final_status["updated_at"]) >= _parse_iso(
        observed_active_status["updated_at"]
    )
