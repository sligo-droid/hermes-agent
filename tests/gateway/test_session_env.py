import asyncio
import os

import pytest

from gateway.config import Platform
from gateway.session import SessionContext, SessionSource
from gateway.session_context import (
    bind_project_inspection_candidates,
    get_session_env,
    reset_project_inspection_candidates,
    set_session_vars,
    clear_session_vars,
    _VAR_MAP,
    _UNSET,
    _bind_trusted_discord_work_item_id,
    get_trusted_discord_work_item_id,
)


def _gateway_runner():
    # Import after the per-test HERMES_HOME fixture has run. Importing
    # gateway.run during collection can cache the developer's live config.
    from gateway.run import GatewayRunner

    return object.__new__(GatewayRunner)


@pytest.fixture(autouse=True)
def _reset_contextvars():
    """Reset all session contextvars to _UNSET between tests.

    In production each asyncio.Task gets a fresh context copy where the
    defaults are _UNSET.  In tests all functions share the same thread
    context, so a clear_session_vars() from test A (which sets vars to "")
    would leak into test B.  This fixture ensures each test starts clean.
    """
    yield
    for var in _VAR_MAP.values():
        # Can't use var.reset() without a token; just set back to sentinel.
        var.set(_UNSET)


def test_set_session_env_sets_contextvars(monkeypatch):
    """_set_session_env should populate contextvars, not os.environ."""
    runner = _gateway_runner()
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_name="Group",
        chat_type="group",
        user_id="123456",
        user_name="alice",
        thread_id="17585",
    )
    context = SessionContext(source=source, connected_platforms=[], home_channels={})

    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_NAME", raising=False)
    monkeypatch.delenv("HERMES_SESSION_USER_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_USER_NAME", raising=False)
    monkeypatch.delenv("HERMES_SESSION_THREAD_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CWD", raising=False)

    tokens = runner._set_session_env(context, session_cwd="/home/droid/hermes")

    # Values should be readable via get_session_env (contextvar path)
    assert get_session_env("HERMES_SESSION_PLATFORM") == "telegram"
    assert get_session_env("HERMES_SESSION_CHAT_ID") == "-1001"
    assert get_session_env("HERMES_SESSION_CHAT_NAME") == "Group"
    assert get_session_env("HERMES_SESSION_USER_ID") == "123456"
    assert get_session_env("HERMES_SESSION_USER_NAME") == "alice"
    assert get_session_env("HERMES_SESSION_THREAD_ID") == "17585"
    assert get_session_env("HERMES_SESSION_CWD") == "/home/droid/hermes"

    # os.environ should NOT be touched
    assert os.getenv("HERMES_SESSION_PLATFORM") is None
    assert os.getenv("HERMES_SESSION_THREAD_ID") is None
    assert os.getenv("HERMES_SESSION_CWD") is None

    # Clean up
    runner._clear_session_env(tokens)


def test_set_session_env_sets_project_contextvars(monkeypatch):
    runner = _gateway_runner()
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="thread-1",
        chat_name="Sligo Labs / #pid / feature",
        chat_type="thread",
        project_key="pid",
        project_name="PID",
        project_path="/home/droid/.hermes/workspace/PID",
        project_github_url="https://github.com/sligo-labs/pid",
        project_channel_id="chan-1",
        project_inspection_candidates=[
            {
                "url": "http://localhost:3000",
                "environment": "development",
                "location": "local",
            }
        ],
    )
    context = SessionContext(source=source, connected_platforms=[], home_channels={})

    for name in (
        "HERMES_PROJECT_KEY",
        "HERMES_PROJECT_PATH",
        "HERMES_PROJECT_NAME",
        "HERMES_PROJECT_GITHUB_URL",
        "HERMES_PROJECT_CHANNEL_ID",
        "HERMES_PROJECT_INSPECTION_CANDIDATES",
    ):
        monkeypatch.delenv(name, raising=False)

    tokens = runner._set_session_env(context)

    assert get_session_env("HERMES_PROJECT_KEY") == "pid"
    assert get_session_env("HERMES_PROJECT_PATH") == "/home/droid/.hermes/workspace/PID"
    assert get_session_env("HERMES_PROJECT_NAME") == "PID"
    assert get_session_env("HERMES_PROJECT_GITHUB_URL") == "https://github.com/sligo-labs/pid"
    assert get_session_env("HERMES_PROJECT_CHANNEL_ID") == "chan-1"
    assert get_session_env("HERMES_PROJECT_INSPECTION_CANDIDATES") == (
        '[{"url":"http://localhost:3000/","environment":"development","location":"local"}]'
    )

    runner._clear_session_env(tokens)
    assert get_session_env("HERMES_PROJECT_KEY") == ""
    assert get_session_env("HERMES_PROJECT_PATH") == ""
    assert get_session_env("HERMES_PROJECT_NAME") == ""
    assert get_session_env("HERMES_PROJECT_INSPECTION_CANDIDATES") == ""


def test_project_inspection_candidate_binding_is_task_local_and_resettable():
    outer = bind_project_inspection_candidates(
        [{"url": "https://outer.example", "environment": "production"}]
    )
    inner = bind_project_inspection_candidates(
        [{"url": "http://localhost:3000", "environment": "development"}]
    )

    assert "localhost:3000" in get_session_env("HERMES_PROJECT_INSPECTION_CANDIDATES")
    reset_project_inspection_candidates(inner)
    assert "outer.example" in get_session_env("HERMES_PROJECT_INSPECTION_CANDIDATES")
    reset_project_inspection_candidates(outer)


def test_set_and_clear_session_vars_cover_project_inspection_candidates():
    tokens = set_session_vars(
        project_key="example",
        project_inspection_candidates=[
            {"url": "http://localhost:3000", "environment": "development"}
        ]
    )

    assert get_session_env("HERMES_PROJECT_KEY") == "example"
    assert "localhost:3000" in get_session_env("HERMES_PROJECT_INSPECTION_CANDIDATES")
    clear_session_vars(tokens)
    assert get_session_env("HERMES_PROJECT_KEY") == ""
    assert get_session_env("HERMES_PROJECT_INSPECTION_CANDIDATES") == ""


def test_clear_session_env_restores_previous_state(monkeypatch):
    """_clear_session_env should restore contextvars to their pre-handler values."""
    runner = _gateway_runner()

    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_NAME", raising=False)
    monkeypatch.delenv("HERMES_SESSION_USER_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_USER_NAME", raising=False)
    monkeypatch.delenv("HERMES_SESSION_THREAD_ID", raising=False)

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_name="Group",
        chat_type="group",
        user_id="123456",
        user_name="alice",
        thread_id="17585",
    )
    context = SessionContext(source=source, connected_platforms=[], home_channels={})

    tokens = runner._set_session_env(context)
    assert get_session_env("HERMES_SESSION_PLATFORM") == "telegram"
    assert get_session_env("HERMES_SESSION_USER_ID") == "123456"

    runner._clear_session_env(tokens)

    # After clear, contextvars should return to defaults (empty)
    assert get_session_env("HERMES_SESSION_PLATFORM") == ""
    assert get_session_env("HERMES_SESSION_CHAT_ID") == ""
    assert get_session_env("HERMES_SESSION_CHAT_NAME") == ""
    assert get_session_env("HERMES_SESSION_USER_ID") == ""
    assert get_session_env("HERMES_SESSION_USER_NAME") == ""
    assert get_session_env("HERMES_SESSION_THREAD_ID") == ""


def test_get_session_env_falls_back_to_os_environ(monkeypatch):
    """get_session_env should fall back to os.environ when contextvar is unset."""
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "discord")

    # No contextvar set — should read from os.environ
    assert get_session_env("HERMES_SESSION_PLATFORM") == "discord"

    # Now set a contextvar — should prefer it
    tokens = set_session_vars(platform="telegram")
    assert get_session_env("HERMES_SESSION_PLATFORM") == "telegram"

    # After clear — should return "" (explicitly cleared), NOT fall back
    # to os.environ.  This is the fix for #10304: stale os.environ values
    # must not leak through after a gateway session is cleaned up.
    clear_session_vars(tokens)
    assert get_session_env("HERMES_SESSION_PLATFORM") == ""


def test_get_session_env_default_when_nothing_set(monkeypatch):
    """get_session_env returns default when neither contextvar nor env is set."""
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)

    assert get_session_env("HERMES_SESSION_PLATFORM") == ""
    assert get_session_env("HERMES_SESSION_PLATFORM", "fallback") == "fallback"


def test_set_session_env_handles_missing_optional_fields():
    """_set_session_env should handle None chat_name and thread_id gracefully."""
    runner = _gateway_runner()
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_name=None,
        chat_type="private",
        thread_id=None,
    )
    context = SessionContext(source=source, connected_platforms=[], home_channels={})

    tokens = runner._set_session_env(context)

    assert get_session_env("HERMES_SESSION_PLATFORM") == "telegram"
    assert get_session_env("HERMES_SESSION_CHAT_ID") == "-1001"
    assert get_session_env("HERMES_SESSION_CHAT_NAME") == ""
    assert get_session_env("HERMES_SESSION_THREAD_ID") == ""

    runner._clear_session_env(tokens)


# ---------------------------------------------------------------------------
# SESSION_KEY contextvars tests
# ---------------------------------------------------------------------------


def test_session_key_set_via_contextvars(monkeypatch):
    """set_session_vars should set HERMES_SESSION_KEY via contextvars."""
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)

    tokens = set_session_vars(
        platform="telegram",
        chat_id="-1001",
        session_key="tg:-1001:17585",
    )
    assert get_session_env("HERMES_SESSION_KEY") == "tg:-1001:17585"

    clear_session_vars(tokens)
    assert get_session_env("HERMES_SESSION_KEY") == ""


def test_session_key_falls_back_to_os_environ(monkeypatch):
    """get_session_env for SESSION_KEY should fall back to os.environ."""
    monkeypatch.setenv("HERMES_SESSION_KEY", "env-session-123")

    # No contextvar set — should read from os.environ
    assert get_session_env("HERMES_SESSION_KEY") == "env-session-123"

    # Set contextvar — should prefer it
    tokens = set_session_vars(session_key="ctx-session-456")
    assert get_session_env("HERMES_SESSION_KEY") == "ctx-session-456"

    # After clear — should return "" (explicitly cleared), not os.environ (#10304)
    clear_session_vars(tokens)
    assert get_session_env("HERMES_SESSION_KEY") == ""


def test_clear_session_vars_clears_session_id_contextvar(monkeypatch):
    """HERMES_SESSION_ID should not leak across gateway turns."""
    monkeypatch.setenv("HERMES_SESSION_ID", "env-session-id")
    from gateway.session_context import _SESSION_ID

    _SESSION_ID.set("ctx-session-id")
    assert get_session_env("HERMES_SESSION_ID") == "ctx-session-id"

    clear_session_vars([])

    assert get_session_env("HERMES_SESSION_ID") == ""


def test_trusted_discord_work_item_has_no_environment_fallback(monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_WORK_ITEM_ID", "forged-work")

    assert get_trusted_discord_work_item_id() == ""
    assert get_session_env("HERMES_SESSION_WORK_ITEM_ID") == "forged-work"

    tokens = set_session_vars()
    _bind_trusted_discord_work_item_id("work-1")
    try:
        assert get_trusted_discord_work_item_id() == "work-1"
    finally:
        clear_session_vars(tokens)

    assert get_trusted_discord_work_item_id() == ""


def test_set_session_env_explicitly_clears_trusted_owner_for_other_turns():
    runner = _gateway_runner()
    discord = SessionContext(
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id="thread-1",
            chat_type="thread",
        ),
        connected_platforms=[],
        home_channels={},
    )
    telegram = SessionContext(
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="chat-1",
            chat_type="dm",
        ),
        connected_platforms=[],
        home_channels={},
    )

    first = runner._set_session_env(
        discord, trusted_discord_work_item_id="work-1"
    )
    assert get_trusted_discord_work_item_id() == "work-1"
    runner._clear_session_env(first)

    second = runner._set_session_env(telegram, trusted_discord_work_item_id="")
    try:
        assert get_trusted_discord_work_item_id() == ""
    finally:
        runner._clear_session_env(second)


def test_trusted_owner_selector_allows_only_discord_action_work():
    from agent.runtime_capabilities import RuntimeMode
    from gateway.platforms.base import MessageEvent, MessageType

    runner = _gateway_runner()
    discord_source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="thread-1",
        chat_type="thread",
    )
    telegram_source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="chat-1",
        chat_type="dm",
    )
    event = MessageEvent(
        text="work",
        message_type=MessageType.TEXT,
        source=discord_source,
        work_item_id="work-1",
    )

    assert runner._trusted_discord_work_item_id_for_turn(
        event, discord_source, RuntimeMode.ACTION
    ) == "work-1"
    assert runner._trusted_discord_work_item_id_for_turn(
        event, discord_source, RuntimeMode.READ_ONLY
    ) == ""
    assert runner._trusted_discord_work_item_id_for_turn(
        event, telegram_source, RuntimeMode.ACTION
    ) == ""
    event.participates_in_work_lifecycle = False
    assert runner._trusted_discord_work_item_id_for_turn(
        event, discord_source, RuntimeMode.ACTION
    ) == ""


def test_set_session_env_includes_session_key():
    """_set_session_env should propagate session_key from SessionContext."""
    runner = _gateway_runner()
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_name="Group",
        chat_type="group",
        thread_id="17585",
    )
    context = SessionContext(
        source=source,
        connected_platforms=[],
        home_channels={},
        session_key="tg:-1001:17585",
    )

    # Capture baseline value before setting (may be non-empty from another
    # test in the same pytest-xdist worker sharing the context).
    tokens = runner._set_session_env(context)
    assert get_session_env("HERMES_SESSION_KEY") == "tg:-1001:17585"
    runner._clear_session_env(tokens)
    # After clearing, the session key must not retain the value we just set.
    # The exact post-clear value depends on context propagation from other
    # tests, so only check that our value was removed, not what replaced it.
    assert get_session_env("HERMES_SESSION_KEY") != "tg:-1001:17585"


def test_session_key_no_race_condition_with_contextvars(monkeypatch):
    """Prove contextvars isolates SESSION_KEY across concurrent async tasks.

    Two tasks set different session keys. With contextvars each task
    reads back its own value. With os.environ the second task would
    overwrite the first (the old bug).
    """
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)

    results = {}

    async def handler(key: str, delay: float):
        tokens = set_session_vars(session_key=key)
        try:
            await asyncio.sleep(delay)
            read_back = get_session_env("HERMES_SESSION_KEY")
            results[key] = read_back
        finally:
            clear_session_vars(tokens)

    async def run():
        task_a = asyncio.create_task(handler("session-A", 0.15))
        await asyncio.sleep(0.05)
        task_b = asyncio.create_task(handler("session-B", 0.05))
        await asyncio.gather(task_a, task_b)

    asyncio.run(run())

    # Both tasks must read back their own session key
    assert results["session-A"] == "session-A", (
        f"Session A got '{results['session-A']}' instead of 'session-A' — race condition!"
    )
    assert results["session-B"] == "session-B", (
        f"Session B got '{results['session-B']}' instead of 'session-B' — race condition!"
    )


@pytest.mark.asyncio
async def test_run_in_executor_with_context_preserves_session_env(monkeypatch):
    """Gateway executor work should inherit session contextvars for tool routing."""
    runner = _gateway_runner()
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_THREAD_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_USER_ID", raising=False)

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="2144471399",
        chat_type="dm",
        user_id="123456",
        user_name="alice",
        thread_id=None,
    )
    context = SessionContext(
        source=source,
        connected_platforms=[],
        home_channels={},
        session_key="agent:main:telegram:dm:2144471399",
    )

    tokens = runner._set_session_env(context)
    try:
        result = await runner._run_in_executor_with_context(
            lambda: {
                "platform": get_session_env("HERMES_SESSION_PLATFORM"),
                "chat_id": get_session_env("HERMES_SESSION_CHAT_ID"),
                "user_id": get_session_env("HERMES_SESSION_USER_ID"),
                "session_key": get_session_env("HERMES_SESSION_KEY"),
            }
        )
    finally:
        runner._clear_session_env(tokens)
        runner._shutdown_executor()

    assert result == {
        "platform": "telegram",
        "chat_id": "2144471399",
        "user_id": "123456",
        "session_key": "agent:main:telegram:dm:2144471399",
    }


@pytest.mark.asyncio
async def test_run_in_executor_with_context_forwards_args():
    """_run_in_executor_with_context should forward *args to the callable."""
    runner = _gateway_runner()

    def add(a, b):
        return a + b

    try:
        result = await runner._run_in_executor_with_context(add, 3, 7)
    finally:
        runner._shutdown_executor()
    assert result == 10


@pytest.mark.asyncio
async def test_run_in_executor_with_context_propagates_exceptions():
    """Exceptions inside the executor should propagate to the caller."""
    runner = _gateway_runner()

    def blow_up():
        raise ValueError("boom")

    try:
        with pytest.raises(ValueError, match="boom"):
            await runner._run_in_executor_with_context(blow_up)
    finally:
        runner._shutdown_executor()


@pytest.mark.asyncio
async def test_run_in_executor_with_context_survives_default_executor_shutdown():
    """Gateway agent work should not depend on asyncio's default executor."""
    runner = _gateway_runner()
    loop = asyncio.get_running_loop()

    await loop.run_in_executor(None, lambda: None)
    await loop.shutdown_default_executor()

    try:
        result = await runner._run_in_executor_with_context(lambda: "ok")
    finally:
        runner._shutdown_executor()

    assert result == "ok"


@pytest.mark.asyncio
async def test_gateway_executor_refuses_resurrection_after_shutdown():
    """A real gateway shutdown must NOT be resurrected by the recreate path.

    _shutdown_executor() means "we're stopping" — the recreate-on-shutdown
    logic exists to survive an *external* teardown of the loop default
    (test_..._survives_default_executor_shutdown), not to undo our own stop.
    """
    runner = _gateway_runner()

    try:
        first = await runner._run_in_executor_with_context(lambda: "first")
        assert first == "first"
        runner._shutdown_executor()

        with pytest.raises(RuntimeError, match="shutting down"):
            await runner._run_in_executor_with_context(lambda: "second")
    finally:
        runner._shutdown_executor()
