import logging
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource


def _source(*, is_bot=True, user_name="Prompt Bot"):
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id="1504252294495998043",
        chat_type="group",
        user_id="142119213815955456",
        user_name=user_name,
        is_bot=is_bot,
        message_id="m1",
    )


def _event(text="Daily dev-loop smoke test: run the no-op smoke check", *, source=None, message_id="m1"):
    return MessageEvent(
        text=text,
        source=source or _source(),
        message_id=message_id,
    )


def test_prompt_bot_smoke_duplicate_is_dropped_without_touching_humans(caplog):
    runner = object.__new__(gateway_run.GatewayRunner)
    runner._discord_prompt_bot_smoke_seen = gateway_run.OrderedDict()

    caplog.set_level(logging.WARNING)
    first, marker = runner._accept_discord_prompt_bot_smoke(_event(), _source())
    duplicate, marker2 = runner._accept_discord_prompt_bot_smoke(
        _event(message_id="m2"),
        _source(),
    )
    human, human_marker = runner._accept_discord_prompt_bot_smoke(
        _event(source=_source(is_bot=False, user_name="Alice"), message_id="m3"),
        _source(is_bot=False, user_name="Alice"),
    )

    assert first is True
    assert duplicate is False
    assert human is True
    assert marker == marker2
    assert human_marker is None
    assert "discord_prompt_bot_smoke_duplicate_dropped" in caplog.text
    assert "Daily dev-loop smoke test" not in caplog.text


@pytest.mark.asyncio
async def test_duplicate_prompt_bot_smoke_returns_before_agent(monkeypatch):
    runner = object.__new__(gateway_run.GatewayRunner)
    runner._discord_prompt_bot_smoke_seen = gateway_run.OrderedDict()
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._is_user_authorized = lambda _source: True
    runner._session_key_for_source = lambda _source: "agent:main:discord:group:1504252294495998043:142119213815955456"
    runner._accept_discord_work_item = lambda *_args, **_kwargs: None
    runner._update_prompt_pending = {}
    runner.pairing_store = MagicMock()
    runner._handle_message_with_agent = AsyncMock(return_value="should not run")
    runner._refresh_active_agent_runtime_status = lambda: None
    runner._begin_session_run_generation = lambda _key: 1
    runner._release_running_agent_state = lambda _key: None

    monkeypatch.setattr(gateway_run, "detect_grill_me_trigger", lambda _text: False)

    first = _event(message_id="m1")
    second = _event(message_id="m2")

    # Seed the smoke key as if the first delivery was admitted; the duplicate
    # should be coalesced before the runner claims a session or starts an agent.
    assert runner._accept_discord_prompt_bot_smoke(first, first.source)[0] is True
    result = await runner._handle_message(second)

    assert result is None
    runner._handle_message_with_agent.assert_not_awaited()


def test_stale_prompt_bot_smoke_warns_on_user_only_zero_api(caplog):
    runner = object.__new__(gateway_run.GatewayRunner)
    runner._session_db = SimpleNamespace(
        get_session=lambda _sid: {
            "message_count": 3,
            "api_call_count": 0,
            "output_tokens": 0,
        }
    )
    runner.session_store = SimpleNamespace(_db=None)

    caplog.set_level(logging.WARNING)
    runner._warn_if_discord_prompt_bot_smoke_stale(
        session_id="20260707_080056_ddba93cb",
        session_key="agent:main:discord:group:1504252294495998043:142119213815955456",
        source=_source(),
        marker="prompt-bot-smoke:daily+dev-loop:abc123",
        message_id="1522167404321574973",
    )

    assert "discord_prompt_bot_smoke_stale_zero_api" in caplog.text
    assert "20260707_080056_ddba93cb" in caplog.text
    assert "prompt-bot-smoke:daily+dev-loop:abc123" in caplog.text
    assert "Daily dev-loop smoke" not in caplog.text


@pytest.mark.asyncio
async def test_stale_watch_scheduled_for_admitted_prompt_bot_smoke(monkeypatch):
    runner = object.__new__(gateway_run.GatewayRunner)
    runner._discord_prompt_bot_smoke_stale_tasks = {}
    runner._recover_telegram_topic_thread_id = lambda _source: None
    runner._cache_session_source = lambda *_args: None
    runner._is_telegram_topic_lane = lambda _source: False
    runner._mark_discord_default_kanban_intake_context = lambda *_args: False
    runner._set_session_env = lambda *_args, **_kwargs: None
    runner._run_agent = AsyncMock(
        return_value={
            "final_response": "ok",
            "messages": [
                {"role": "user", "content": "Daily dev-loop smoke test: run the no-op smoke check"},
                {"role": "assistant", "content": "ok"},
            ],
            "history_offset": 0,
            "tools": [],
            "last_prompt_tokens": 0,
        }
    )
    runner._is_session_run_current = lambda *_args: True
    runner._reply_anchor_for_event = lambda _event: None
    runner._get_guild_id = lambda _event: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    runner._post_turn_goal_continuation = AsyncMock()
    runner._session_db = None
    runner.adapters = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock())
    runner.config = GatewayConfig()
    session_entry = SessionEntry(
        session_key="agent:main:discord:group:1504252294495998043:142119213815955456",
        session_id="sess-smoke",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.DISCORD,
        chat_type="group",
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store.load_transcript.return_value = []
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.update_session = MagicMock()

    scheduled = []
    monkeypatch.setattr(
        runner,
        "_schedule_discord_prompt_bot_smoke_stale_watch",
        lambda **kwargs: scheduled.append(kwargs),
    )
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "build_session_context", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(gateway_run, "build_session_context_prompt", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(gateway_run, "_resolve_gateway_session_cwd", lambda *_args, **_kwargs: None)

    await runner._handle_message_with_agent(
        _event(),
        _source(),
        "agent:main:discord:group:1504252294495998043:142119213815955456",
        1,
    )

    assert scheduled
    assert scheduled[0]["session_id"] == "sess-smoke"
    assert scheduled[0]["marker"].startswith("prompt-bot-smoke:")
