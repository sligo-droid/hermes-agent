"""Tests for configurable background process notification modes.

The gateway process watcher pushes status updates to users' chats when
background terminal commands run.  ``display.background_process_notifications``
controls verbosity: off | result | error | all (default).

Contributed by @PeterFile (PR #593), reimplemented on current main.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.run import GatewayRunner, _parse_session_key


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeRegistry:
    """Return pre-canned sessions, then None once exhausted."""

    def __init__(self, sessions, *, consumed=()):
        self._sessions = list(sessions)
        self._consumed = set(consumed)

    def get(self, session_id):
        if self._sessions:
            return self._sessions.pop(0)
        return None

    def is_completion_consumed(self, session_id):
        return session_id in self._consumed


def _build_runner(monkeypatch, tmp_path, mode: str) -> GatewayRunner:
    """Create a GatewayRunner with a fake config for the given mode."""
    (tmp_path / "config.yaml").write_text(
        f"display:\n  background_process_notifications: {mode}\n",
        encoding="utf-8",
    )

    import gateway.run as gateway_run

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner = GatewayRunner(GatewayConfig())
    adapter = SimpleNamespace(send=AsyncMock(), handle_message=AsyncMock())
    runner.adapters[Platform.TELEGRAM] = adapter
    return runner


def _watcher_dict(session_id="proc_test", thread_id=""):
    d = {
        "session_id": session_id,
        "check_interval": 0,
        "platform": "telegram",
        "chat_id": "123",
    }
    if thread_id:
        d["thread_id"] = thread_id
    return d


# ---------------------------------------------------------------------------
# _load_background_notifications_mode unit tests
# ---------------------------------------------------------------------------

class TestLoadBackgroundNotificationsMode:

    def test_defaults_to_all(self, monkeypatch, tmp_path):
        import gateway.run as gw
        monkeypatch.setattr(gw, "_hermes_home", tmp_path)
        monkeypatch.delenv("HERMES_BACKGROUND_NOTIFICATIONS", raising=False)
        assert GatewayRunner._load_background_notifications_mode() == "all"

    def test_reads_config_yaml(self, monkeypatch, tmp_path):
        (tmp_path / "config.yaml").write_text(
            "display:\n  background_process_notifications: error\n"
        )
        import gateway.run as gw
        monkeypatch.setattr(gw, "_hermes_home", tmp_path)
        monkeypatch.delenv("HERMES_BACKGROUND_NOTIFICATIONS", raising=False)
        assert GatewayRunner._load_background_notifications_mode() == "error"

    def test_env_var_overrides_config(self, monkeypatch, tmp_path):
        (tmp_path / "config.yaml").write_text(
            "display:\n  background_process_notifications: error\n"
        )
        import gateway.run as gw
        monkeypatch.setattr(gw, "_hermes_home", tmp_path)
        monkeypatch.setenv("HERMES_BACKGROUND_NOTIFICATIONS", "off")
        assert GatewayRunner._load_background_notifications_mode() == "off"

    def test_false_value_maps_to_off(self, monkeypatch, tmp_path):
        (tmp_path / "config.yaml").write_text(
            "display:\n  background_process_notifications: false\n"
        )
        import gateway.run as gw
        monkeypatch.setattr(gw, "_hermes_home", tmp_path)
        monkeypatch.delenv("HERMES_BACKGROUND_NOTIFICATIONS", raising=False)
        assert GatewayRunner._load_background_notifications_mode() == "off"

    def test_invalid_value_defaults_to_all(self, monkeypatch, tmp_path):
        (tmp_path / "config.yaml").write_text(
            "display:\n  background_process_notifications: banana\n"
        )
        import gateway.run as gw
        monkeypatch.setattr(gw, "_hermes_home", tmp_path)
        monkeypatch.delenv("HERMES_BACKGROUND_NOTIFICATIONS", raising=False)
        assert GatewayRunner._load_background_notifications_mode() == "all"


# ---------------------------------------------------------------------------
# _run_process_watcher integration tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "sessions", "expected_calls", "expected_fragment"),
    [
        # all mode: running output → sends update
        (
            "all",
            [
                SimpleNamespace(output_buffer="building...\n", exited=False, exit_code=None),
                None,  # process disappears → watcher exits
            ],
            1,
            "is still running",
        ),
        # result mode: running output → no update
        (
            "result",
            [
                SimpleNamespace(output_buffer="building...\n", exited=False, exit_code=None),
                None,
            ],
            0,
            None,
        ),
        # off mode: exited process → no notification
        (
            "off",
            [SimpleNamespace(output_buffer="done\n", exited=True, exit_code=0)],
            0,
            None,
        ),
        # result mode: exited → notifies
        (
            "result",
            [SimpleNamespace(output_buffer="done\n", exited=True, exit_code=0)],
            1,
            "finished with exit code 0",
        ),
        # error mode: exit 0 → no notification
        (
            "error",
            [SimpleNamespace(output_buffer="done\n", exited=True, exit_code=0)],
            0,
            None,
        ),
        # error mode: exit 1 → notifies
        (
            "error",
            [SimpleNamespace(output_buffer="traceback\n", exited=True, exit_code=1)],
            1,
            "finished with exit code 1",
        ),
        # all mode: exited → notifies
        (
            "all",
            [SimpleNamespace(output_buffer="ok\n", exited=True, exit_code=0)],
            1,
            "finished with exit code 0",
        ),
    ],
)
async def test_run_process_watcher_respects_notification_mode(
    monkeypatch, tmp_path, mode, sessions, expected_calls, expected_fragment
):
    import tools.process_registry as pr_module

    monkeypatch.setattr(pr_module, "process_registry", _FakeRegistry(sessions))

    # Patch asyncio.sleep to avoid real delays
    async def _instant_sleep(*_a, **_kw):
        pass
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)

    runner = _build_runner(monkeypatch, tmp_path, mode)
    adapter = runner.adapters[Platform.TELEGRAM]

    await runner._run_process_watcher(_watcher_dict())

    assert adapter.send.await_count == expected_calls, (
        f"mode={mode}: expected {expected_calls} sends, got {adapter.send.await_count}"
    )
    if expected_fragment is not None:
        sent_message = adapter.send.await_args.args[1]
        assert expected_fragment in sent_message


@pytest.mark.asyncio
async def test_completion_notice_suppresses_process_output(monkeypatch, tmp_path):
    """Direct chat completion notices should not dump process output tails."""
    import tools.process_registry as pr_module

    sessions = [
        SimpleNamespace(
            output_buffer="Traceback (most recent call last):\nSECRET_OR_NOISY_DETAIL\n",
            exited=True,
            exit_code=1,
        )
    ]
    monkeypatch.setattr(pr_module, "process_registry", _FakeRegistry(sessions))

    async def _instant_sleep(*_a, **_kw):
        pass
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)

    runner = _build_runner(monkeypatch, tmp_path, "result")
    adapter = runner.adapters[Platform.TELEGRAM]

    await runner._run_process_watcher(_watcher_dict())

    adapter.send.assert_awaited_once()
    sent_message = adapter.send.await_args.args[1]
    assert "finished with exit code 1" in sent_message
    assert "was not posted to chat" in sent_message
    assert "Traceback" not in sent_message
    assert "SECRET_OR_NOISY_DETAIL" not in sent_message


@pytest.mark.asyncio
async def test_consumed_agent_completion_does_not_post_a_second_notice(
    monkeypatch, tmp_path,
):
    """A result already returned through ``process wait/log`` needs no chat echo."""
    import tools.process_registry as pr_module

    session_id = "proc_already_consumed"
    sessions = [
        SimpleNamespace(
            output_buffer="health check passed\n",
            exited=True,
            exit_code=0,
            command="check-health",
        )
    ]
    monkeypatch.setattr(
        pr_module,
        "process_registry",
        _FakeRegistry(sessions, consumed={session_id}),
    )

    async def _instant_sleep(*_a, **_kw):
        pass
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)

    runner = _build_runner(monkeypatch, tmp_path, "all")
    adapter = runner.adapters[Platform.TELEGRAM]
    watcher = _watcher_dict(session_id)
    watcher["notify_on_complete"] = True

    await runner._run_process_watcher(watcher)

    adapter.handle_message.assert_not_awaited()
    adapter.send.assert_not_awaited()


@pytest.mark.parametrize(
    "status, expected",
    [
        ("response_delivered", True),
        ("summary_updated", True),
        ("completed", True),
        ("failed", True),
        ("agent_running", False),
        ("agent_done", False),
    ],
)
def test_stale_discord_process_completion_only_drops_after_final_delivery(
    status, expected,
):
    """A late terminal result cannot reopen an already-delivered action item."""
    from gateway.platforms.base import MessageEvent, MessageType
    from gateway.session import SessionSource

    runner = object.__new__(GatewayRunner)
    runner._ledger = lambda: SimpleNamespace(get=lambda _work_id: {"status": status})
    event = MessageEvent(
        text="[IMPORTANT: Background process finished]",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id="thread-1",
            chat_type="thread",
            thread_id="thread-1",
        ),
        internal=True,
        work_item_id="work-1",
        background_process_completion=True,
    )

    assert runner._is_stale_discord_process_completion(
        event,
        "agent:main:discord:thread:thread-1:thread-1",
    ) is expected


def test_consumed_queued_process_completion_is_redundant(monkeypatch):
    """A watcher race must not create a second turn after wait/log consumed it."""
    import tools.process_registry as pr_module
    from gateway.platforms.base import MessageEvent

    monkeypatch.setattr(
        pr_module,
        "process_registry",
        _FakeRegistry([], consumed={"proc-consumed"}),
    )
    event = MessageEvent(
        text="[IMPORTANT: Background process exited]",
        internal=True,
        background_process_completion=True,
        background_process_session_id="proc-consumed",
    )

    assert GatewayRunner._is_consumed_process_completion(event) is True


def test_unconsumed_queued_process_completion_still_continues(monkeypatch):
    """Unobserved completions retain notify_on_complete continuation semantics."""
    import tools.process_registry as pr_module
    from gateway.platforms.base import MessageEvent

    monkeypatch.setattr(pr_module, "process_registry", _FakeRegistry([]))
    event = MessageEvent(
        text="[IMPORTANT: Background process exited]",
        internal=True,
        background_process_completion=True,
        background_process_session_id="proc-unconsumed",
    )

    assert GatewayRunner._is_consumed_process_completion(event) is False


@pytest.mark.asyncio
async def test_tagged_process_completion_hydrates_exact_w1_not_newer_w2():
    from gateway.platforms.base import MessageType
    from gateway.session import SessionSource

    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="thread-1",
        chat_type="thread",
        thread_id="thread-1",
    )
    items = {
        "work-1": {
            "id": "work-1",
            "status": "agent_running",
            "platform": "discord",
            "discord_runtime_mode": "action",
        },
        "work-2": {
            "id": "work-2",
            "status": "agent_running",
            "platform": "discord",
            "discord_runtime_mode": "action",
        },
    }
    lookups = []
    ledger = SimpleNamespace(
        get=lambda work_id: lookups.append(work_id) or items.get(work_id),
        id_for_event=lambda *_args: (_ for _ in ()).throw(
            AssertionError("tagged completion must not use session fallback")
        ),
    )
    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = object.__new__(GatewayRunner)
    runner._completion_suppressed_by_explicit_stop = lambda _evt: False
    runner._build_process_event_source = lambda _evt: source
    runner._adapter_for_source = lambda _source: adapter
    runner._ledger = lambda: ledger

    delivered = await runner._inject_process_completion(
        "[IMPORTANT: done]",
        {
            "type": "completion",
            "session_id": "proc-1",
            "session_key": "shared-session",
            "origin_work_item_id": "work-1",
        },
    )

    assert delivered is True
    event = adapter.handle_message.await_args.args[0]
    assert event.message_type is MessageType.TEXT
    assert event.work_item_id == "work-1"
    assert "work-2" not in lookups


@pytest.mark.asyncio
async def test_delivered_tagged_w1_is_stale_without_consulting_active_w2():
    from gateway.session import SessionSource

    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="thread-1",
        chat_type="thread",
        thread_id="thread-1",
    )
    items = {
        "work-1": {
            "id": "work-1",
            "status": "response_delivered",
            "platform": "discord",
            "discord_runtime_mode": "action",
        },
        "work-2": {
            "id": "work-2",
            "status": "agent_running",
            "platform": "discord",
            "discord_runtime_mode": "action",
        },
    }
    lookups = []
    ledger = SimpleNamespace(
        get=lambda work_id: lookups.append(work_id) or items.get(work_id),
        id_for_event=lambda *_args: (_ for _ in ()).throw(
            AssertionError("tagged completion must not use session fallback")
        ),
    )
    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = object.__new__(GatewayRunner)
    runner._completion_suppressed_by_explicit_stop = lambda _evt: False
    runner._build_process_event_source = lambda _evt: source
    runner._adapter_for_source = lambda _source: adapter
    runner._ledger = lambda: ledger

    assert await runner._inject_process_completion(
        "[IMPORTANT: done]",
        {
            "type": "completion",
            "session_id": "proc-1",
            "session_key": "shared-session",
            "origin_work_item_id": "work-1",
        },
    ) is True
    event = adapter.handle_message.await_args.args[0]
    assert event.work_item_id == "work-1"
    assert runner._is_stale_discord_process_completion(
        event, "shared-session"
    ) is True
    assert "work-2" not in lookups


@pytest.mark.asyncio
async def test_tagged_process_completion_missing_w1_never_falls_back_to_w2():
    from gateway.session import SessionSource

    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="thread-1",
        chat_type="thread",
        thread_id="thread-1",
    )
    lookups = []
    ledger = SimpleNamespace(
        get=lambda work_id: lookups.append(work_id) or None,
        id_for_event=lambda *_args: (_ for _ in ()).throw(
            AssertionError("missing tagged owner must not use session fallback")
        ),
    )
    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = object.__new__(GatewayRunner)
    runner._completion_suppressed_by_explicit_stop = lambda _evt: False
    runner._build_process_event_source = lambda _evt: source
    runner._adapter_for_source = lambda _source: adapter
    runner._ledger = lambda: ledger

    delivered = await runner._inject_process_completion(
        "[IMPORTANT: done]",
        {
            "type": "completion",
            "session_id": "proc-1",
            "session_key": "shared-session",
            "origin_work_item_id": "work-missing",
        },
    )

    assert delivered is None
    assert lookups == ["work-missing"]
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_tagged_process_owner_lookup_exception_is_retryable():
    from gateway.session import SessionSource

    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="thread-1",
        chat_type="thread",
        thread_id="thread-1",
    )
    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = object.__new__(GatewayRunner)
    runner._completion_suppressed_by_explicit_stop = lambda _evt: False
    runner._build_process_event_source = lambda _evt: source
    runner._adapter_for_source = lambda _source: adapter
    runner._ledger = lambda: SimpleNamespace(
        get=lambda _work_id: (_ for _ in ()).throw(OSError("database busy"))
    )

    delivered = await runner._inject_process_completion(
        "[IMPORTANT: done]",
        {
            "type": "completion",
            "session_id": "proc-1",
            "session_key": "shared-session",
            "origin_work_item_id": "work-1",
        },
    )

    assert delivered is False
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_untagged_process_completion_keeps_session_fallback():
    from gateway.session import SessionSource

    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="thread-1",
        chat_type="thread",
        thread_id="thread-1",
    )
    fallback = []
    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = object.__new__(GatewayRunner)
    runner._completion_suppressed_by_explicit_stop = lambda _evt: False
    runner._build_process_event_source = lambda _evt: source
    runner._adapter_for_source = lambda _source: adapter
    runner._hydrate_discord_continuation_event_from_work_item = (
        lambda _event, _session_key, *, allow_session_fallback: fallback.append(
            allow_session_fallback
        )
    )

    delivered = await runner._inject_process_completion(
        "[IMPORTANT: done]",
        {
            "type": "completion",
            "session_id": "proc-legacy",
            "session_key": "shared-session",
        },
    )

    assert delivered is True
    assert fallback == [True]


def test_process_completion_dedupe_identity_ignores_work_item_provenance():
    base = {
        "type": "completion",
        "session_id": "proc-1",
        "started_at": 123.5,
    }

    assert GatewayRunner._completion_delivery_identity(
        {**base, "origin_work_item_id": "work-1"}
    ) == GatewayRunner._completion_delivery_identity(
        {**base, "origin_work_item_id": "work-2"}
    )


@pytest.mark.asyncio
async def test_thread_id_passed_to_send(monkeypatch, tmp_path):
    """thread_id from watcher dict is forwarded as metadata to adapter.send()."""
    import tools.process_registry as pr_module

    sessions = [SimpleNamespace(output_buffer="done\n", exited=True, exit_code=0)]
    monkeypatch.setattr(pr_module, "process_registry", _FakeRegistry(sessions))

    async def _instant_sleep(*_a, **_kw):
        pass
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)

    runner = _build_runner(monkeypatch, tmp_path, "all")
    adapter = runner.adapters[Platform.TELEGRAM]

    await runner._run_process_watcher(_watcher_dict(thread_id="42"))

    assert adapter.send.await_count == 1
    _, kwargs = adapter.send.call_args
    assert kwargs["metadata"] == {"thread_id": "42"}


@pytest.mark.asyncio
async def test_no_thread_id_sends_no_metadata(monkeypatch, tmp_path):
    """When thread_id is empty, metadata should be None (general topic)."""
    import tools.process_registry as pr_module

    sessions = [SimpleNamespace(output_buffer="done\n", exited=True, exit_code=0)]
    monkeypatch.setattr(pr_module, "process_registry", _FakeRegistry(sessions))

    async def _instant_sleep(*_a, **_kw):
        pass
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)

    runner = _build_runner(monkeypatch, tmp_path, "all")
    adapter = runner.adapters[Platform.TELEGRAM]

    await runner._run_process_watcher(_watcher_dict())

    assert adapter.send.await_count == 1
    _, kwargs = adapter.send.call_args
    assert kwargs["metadata"] is None


@pytest.mark.asyncio
async def test_inject_watch_notification_sends_direct_notice_from_session_store_origin(monkeypatch, tmp_path):
    from gateway.session import SessionSource

    runner = _build_runner(monkeypatch, tmp_path, "all")
    adapter = runner.adapters[Platform.TELEGRAM]
    runner.session_store._entries["agent:main:telegram:group:-100:42"] = SimpleNamespace(
        origin=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="-100",
            chat_type="group",
            thread_id="42",
            user_id="123",
            user_name="Emiliyan",
        )
    )

    evt = {
        "session_id": "proc_watch",
        "session_key": "agent:main:telegram:group:-100:42",
    }

    await runner._inject_watch_notification("[SYSTEM: Background process matched]", evt)

    adapter.handle_message.assert_not_awaited()
    adapter.send.assert_awaited_once_with(
        "-100",
        "[SYSTEM: Background process matched]",
        metadata={"thread_id": "42"},
    )


@pytest.mark.asyncio
async def test_agent_notification_carries_message_id_reply_anchor(monkeypatch, tmp_path):
    """notify_on_complete injection carries the triggering message_id so the
    synthetic event can be reply-anchored back into a Telegram DM topic.

    Without an anchor, Telegram private-chat topic sends fall back to the main
    chat (see _thread_kwargs_for_send / telegram_dm_topic_reply_fallback)."""
    import tools.process_registry as pr_module

    sessions = [SimpleNamespace(
        output_buffer="SMOKE_OK\n", exited=True, exit_code=0, command="sleep 1",
    )]
    monkeypatch.setattr(pr_module, "process_registry", _FakeRegistry(sessions))

    async def _instant_sleep(*_a, **_kw):
        pass
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)

    runner = _build_runner(monkeypatch, tmp_path, "all")
    adapter = runner.adapters[Platform.TELEGRAM]

    watcher = {
        "session_id": "proc_anchor",
        "check_interval": 0,
        "session_key": "agent:main:telegram:dm:123:24296",
        "platform": "telegram",
        "chat_id": "123",
        "thread_id": "24296",
        "message_id": "555",
        "notify_on_complete": True,
    }
    await runner._run_process_watcher(watcher)

    adapter.handle_message.assert_awaited_once()
    adapter.send.assert_not_awaited()
    synth_event = adapter.handle_message.await_args.args[0]
    assert synth_event.internal is True
    assert synth_event.message_id == "555"
    assert synth_event.source.thread_id == "24296"


@pytest.mark.asyncio
async def test_agent_notification_no_message_id_is_tolerated(monkeypatch, tmp_path):
    """A watcher dict without message_id (CLI spawn, pre-upgrade checkpoint)
    still injects — message_id is simply None."""
    import tools.process_registry as pr_module

    sessions = [SimpleNamespace(
        output_buffer="done\n", exited=True, exit_code=0, command="sleep 1",
    )]
    monkeypatch.setattr(pr_module, "process_registry", _FakeRegistry(sessions))

    async def _instant_sleep(*_a, **_kw):
        pass
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)

    runner = _build_runner(monkeypatch, tmp_path, "all")
    adapter = runner.adapters[Platform.TELEGRAM]

    watcher = {
        "session_id": "proc_anchorless",
        "check_interval": 0,
        "session_key": "agent:main:telegram:dm:123:24296",
        "platform": "telegram",
        "chat_id": "123",
        "thread_id": "24296",
        "notify_on_complete": True,
    }
    await runner._run_process_watcher(watcher)

    adapter.handle_message.assert_awaited_once()
    synth_event = adapter.handle_message.await_args.args[0]
    assert synth_event.message_id is None


@pytest.mark.asyncio
async def test_inject_watch_notification_sends_to_thread_without_agent_turn(monkeypatch, tmp_path):
    from gateway.session import SessionSource

    runner = _build_runner(monkeypatch, tmp_path, "all")
    adapter = runner.adapters[Platform.TELEGRAM]
    runner.session_store._entries["agent:main:telegram:dm:123:24296"] = SimpleNamespace(
        origin=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="123",
            chat_type="dm",
            thread_id="24296",
            user_id="1",
            user_name="Fabio",
        )
    )

    evt = {
        "session_id": "proc_watch",
        "session_key": "agent:main:telegram:dm:123:24296",
        "message_id": "777",
    }

    await runner._inject_watch_notification("[SYSTEM: Background process matched]", evt)

    adapter.handle_message.assert_not_awaited()
    adapter.send.assert_awaited_once_with(
        "123",
        "[SYSTEM: Background process matched]",
        metadata={"thread_id": "24296"},
    )


def test_build_process_event_source_falls_back_to_session_key_chat_type(monkeypatch, tmp_path):
    runner = _build_runner(monkeypatch, tmp_path, "all")

    evt = {
        "session_id": "proc_watch",
        "session_key": "agent:main:telegram:group:-100:42",
        "platform": "telegram",
        "chat_id": "-100",
        "thread_id": "42",
        "user_id": "123",
        "user_name": "Emiliyan",
    }

    source = runner._build_process_event_source(evt)

    assert source is not None
    assert source.platform == Platform.TELEGRAM
    assert source.chat_id == "-100"
    assert source.chat_type == "group"
    assert source.thread_id == "42"
    assert source.user_id == "123"
    assert source.user_name == "Emiliyan"


def test_build_process_event_source_uses_cached_live_source_before_session_key_parse(
    monkeypatch, tmp_path
):
    from gateway.session import SessionSource

    runner = _build_runner(monkeypatch, tmp_path, "all")
    runner._cache_session_source(
        "agent:main:telegram:group:-100:42",
        SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="-100",
            chat_type="group",
            thread_id="42",
            user_id="proc_owner",
            user_name="alice",
        ),
    )

    source = runner._build_process_event_source(
        {
            "session_id": "proc_watch",
            "session_key": "agent:main:telegram:group:-100:42",
        }
    )

    assert source is not None
    assert source.platform == Platform.TELEGRAM
    assert source.chat_id == "-100"
    assert source.chat_type == "group"
    assert source.thread_id == "42"
    assert source.user_id == "proc_owner"
    assert source.user_name == "alice"


@pytest.mark.asyncio
async def test_inject_watch_notification_ignores_foreground_event_source(monkeypatch, tmp_path):
    """Negative test: watch notification must NOT route to the foreground thread."""
    from gateway.session import SessionSource

    runner = _build_runner(monkeypatch, tmp_path, "all")
    adapter = runner.adapters[Platform.TELEGRAM]

    # Session store has the process's original thread (thread 42)
    runner.session_store._entries["agent:main:telegram:group:-100:42"] = SimpleNamespace(
        origin=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="-100",
            chat_type="group",
            thread_id="42",
            user_id="proc_owner",
            user_name="alice",
        )
    )

    # The evt dict carries the correct session_key — NOT a foreground event
    evt = {
        "session_id": "proc_cross_thread",
        "session_key": "agent:main:telegram:group:-100:42",
    }

    await runner._inject_watch_notification("[SYSTEM: watch match]", evt)

    adapter.handle_message.assert_not_awaited()
    # Must route to thread 42 (process origin), NOT some other thread
    adapter.send.assert_awaited_once_with(
        "-100",
        "[SYSTEM: watch match]",
        metadata={"thread_id": "42"},
    )


@pytest.mark.asyncio
async def test_inject_watch_notification_respects_result_mode(monkeypatch, tmp_path):
    """Non-"all" background notification modes suppress watch-match chatter.

    Watch matches are intermediate process status, not final results. In result
    mode they should not trigger either a direct chat message or a synthetic
    agent turn.
    """
    from gateway.session import SessionSource

    runner = _build_runner(monkeypatch, tmp_path, "result")
    adapter = runner.adapters[Platform.TELEGRAM]
    runner.session_store._entries["agent:main:telegram:group:-100:42"] = SimpleNamespace(
        origin=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="-100",
            chat_type="group",
            thread_id="42",
            user_id="proc_owner",
            user_name="alice",
        )
    )

    await runner._inject_watch_notification(
        "[SYSTEM: watch match]",
        {
            "session_id": "proc_result_mode",
            "session_key": "agent:main:telegram:group:-100:42",
        },
    )

    adapter.handle_message.assert_not_awaited()
    adapter.send.assert_not_awaited()


def test_build_process_event_source_returns_none_for_empty_evt(monkeypatch, tmp_path):
    """Missing session_key and no platform metadata → None (drop notification)."""
    runner = _build_runner(monkeypatch, tmp_path, "all")

    source = runner._build_process_event_source({"session_id": "proc_orphan"})
    assert source is None


def test_build_process_event_source_returns_none_for_invalid_platform(monkeypatch, tmp_path):
    """Invalid platform string → None."""
    runner = _build_runner(monkeypatch, tmp_path, "all")

    evt = {
        "session_id": "proc_bad",
        "platform": "not_a_real_platform",
        "chat_type": "dm",
        "chat_id": "123",
    }
    source = runner._build_process_event_source(evt)
    assert source is None


def test_build_process_event_source_returns_none_for_short_session_key(monkeypatch, tmp_path):
    """Session key with <5 parts doesn't parse, falls through to empty metadata → None."""
    runner = _build_runner(monkeypatch, tmp_path, "all")

    evt = {
        "session_id": "proc_short",
        "session_key": "agent:main:telegram",  # Too few parts
    }
    source = runner._build_process_event_source(evt)
    assert source is None


# ---------------------------------------------------------------------------
# _parse_session_key helper
# ---------------------------------------------------------------------------

def test_parse_session_key_valid():
    result = _parse_session_key("agent:main:telegram:group:-100")
    assert result == {"platform": "telegram", "chat_type": "group", "chat_id": "-100"}


def test_parse_session_key_with_extra_parts():
    """6th part in a group key may be a user_id, not a thread_id — omit it."""
    result = _parse_session_key("agent:main:discord:group:chan123:thread456")
    assert result == {"platform": "discord", "chat_type": "group", "chat_id": "chan123"}


def test_parse_session_key_with_user_id_part():
    """Group keys with per-user isolation have user_id as 6th part — don't return as thread_id."""
    result = _parse_session_key("agent:main:telegram:group:chat1:user99")
    assert result == {"platform": "telegram", "chat_type": "group", "chat_id": "chat1"}


def test_parse_session_key_dm_with_thread():
    """DM keys use parts[5] as thread_id unambiguously."""
    result = _parse_session_key("agent:main:telegram:dm:chat1:topic42")
    assert result == {"platform": "telegram", "chat_type": "dm", "chat_id": "chat1", "thread_id": "topic42"}


def test_parse_session_key_thread_chat_type():
    """Thread-typed keys use parts[5] as thread_id unambiguously."""
    result = _parse_session_key("agent:main:discord:thread:chan1:thread99")
    assert result == {"platform": "discord", "chat_type": "thread", "chat_id": "chan1", "thread_id": "thread99"}


def test_parse_session_key_too_short():
    assert _parse_session_key("agent:main:telegram") is None
    assert _parse_session_key("") is None


def test_parse_session_key_wrong_prefix():
    assert _parse_session_key("cron:main:telegram:dm:123") is None
    assert _parse_session_key("agent:cron:telegram:dm:123") is None
