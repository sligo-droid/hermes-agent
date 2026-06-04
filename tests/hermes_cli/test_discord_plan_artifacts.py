from __future__ import annotations

from pathlib import Path

from hermes_cli.discord_plan_artifacts import (
    lookup_discord_plan_artifact,
    persist_discord_plan_artifact,
    should_persist_discord_plan,
)
from hermes_cli.discord_thread_context import expand_discord_thread_references


THREAD_ID = "1511795999700680744"
PARENT_ID = "1504252294495998043"
GUILD_ID = "1502787243230756904"
BOT_MESSAGE_ID = "1511804847425458217"


def _long_plan() -> str:
    return "## Implementation plan\n" + "\n".join(
        f"{idx}. Phase {idx}: build, verify, and ship the Discord artifact resolver."
        for idx in range(1, 90)
    )


def test_should_persist_long_structured_plan_only():
    assert should_persist_discord_plan(_long_plan(), chunk_count=2)
    assert not should_persist_discord_plan("Done. PR: https://example.test/pr/1", chunk_count=1)


def test_persist_and_lookup_discord_plan_artifact(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    record = persist_discord_plan_artifact(
        _long_plan(),
        thread_id=THREAD_ID,
        channel_id=THREAD_ID,
        guild_id=GUILD_ID,
        parent_channel_id=PARENT_ID,
        source_message_id=THREAD_ID,
        bot_message_ids=[BOT_MESSAGE_ID],
        chunk_count=2,
    )

    assert record is not None
    artifact_path = Path(record.artifact_path)
    assert artifact_path.exists()
    assert str(artifact_path).startswith(str(tmp_path))
    assert record.thread_id == THREAD_ID
    assert record.bot_message_ids == (BOT_MESSAGE_ID,)

    by_thread = lookup_discord_plan_artifact(THREAD_ID)
    by_message = lookup_discord_plan_artifact(BOT_MESSAGE_ID)
    assert by_thread is not None
    assert by_message is not None
    assert by_thread.artifact_path == record.artifact_path
    assert by_message.artifact_path == record.artifact_path
    assert "Discord artifact resolver" in by_thread.content


def test_thread_reference_uses_artifact_without_discord_token(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    record = persist_discord_plan_artifact(
        _long_plan(),
        thread_id=THREAD_ID,
        channel_id=THREAD_ID,
        guild_id=GUILD_ID,
        parent_channel_id=PARENT_ID,
        source_message_id=THREAD_ID,
        bot_message_ids=[BOT_MESSAGE_ID],
        chunk_count=2,
    )
    assert record is not None
    url = f"https://discord.com/channels/{GUILD_ID}/{PARENT_ID}/{THREAD_ID}"

    expansions = expand_discord_thread_references(url, token="", request_func=lambda *args, **kwargs: None)

    assert len(expansions) == 1
    expansion = expansions[0]
    assert expansion.thread_id == THREAD_ID
    assert expansion.artifact_path == record.artifact_path
    assert expansion.content_sha256 == record.content_sha256
    assert expansion.selected_message_ids == (BOT_MESSAGE_ID,)
    assert "Discord artifact resolver" in expansion.content
    formatted = expansion.formatted()
    assert "Artifact:" in formatted
    assert "DISCORD_BOT_TOKEN is not set" not in formatted
