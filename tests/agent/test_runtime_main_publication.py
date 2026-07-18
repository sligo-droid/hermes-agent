from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

import agent.conversation_compression as conversation_compression


# The existing uncommitted turn-context extraction imports this not-yet-wired
# helper. Stub only that unrelated dependency so this focused ordering test can
# exercise the prologue without changing the surrounding executor work.
if not hasattr(conversation_compression, "conversation_history_after_compression"):
    conversation_compression.conversation_history_after_compression = lambda value: value

from agent.turn_context import build_turn_context


class _StopAfterPublication(Exception):
    pass


class _Agent:
    session_id = "session"
    provider = "fallback-provider"
    model = "fallback-model"
    base_url = "https://fallback.invalid/v1"
    api_key = "fallback-key"
    api_mode = "chat_completions"
    _memory_write_origin = "assistant_tool"

    def __init__(self, events):
        self.events = events

    def _restore_primary_runtime(self):
        self.events.append("restore")
        self.provider = "primary-provider"
        self.model = "primary-model"
        self.base_url = "https://primary.invalid/v1"
        self.api_key = "primary-key"
        self.api_mode = "responses"


def test_turn_publishes_only_after_primary_runtime_restoration():
    events = []
    agent = _Agent(events)

    def publish(active_agent):
        events.append(
            (
                "publish",
                active_agent.provider,
                active_agent.model,
                active_agent.base_url,
                active_agent.api_key,
                active_agent.api_mode,
            )
        )

    with patch("agent.auxiliary_client.publish_runtime_main", side_effect=publish):
        with pytest.raises(_StopAfterPublication):
            build_turn_context(
                agent,
                "hello",
                None,
                None,
                None,
                None,
                None,
                restore_or_build_system_prompt=lambda *args, **kwargs: None,
                install_safe_stdio=lambda: None,
                sanitize_surrogates=lambda value: (_ for _ in ()).throw(_StopAfterPublication()),
                summarize_user_message_for_log=lambda value: value,
                set_session_context=lambda value: None,
                set_current_write_origin=lambda value: None,
                ra=lambda: SimpleNamespace(),
            )

    assert events == [
        "restore",
        (
            "publish",
            "primary-provider",
            "primary-model",
            "https://primary.invalid/v1",
            "primary-key",
            "responses",
        ),
    ]
