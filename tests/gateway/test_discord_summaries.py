"""Tests for Discord project and feature summary surfaces."""

import inspect
import sys
import types
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource
import gateway.run as gateway_run


def _ensure_discord_mock():
    if "discord" in sys.modules and hasattr(sys.modules["discord"], "__file__"):
        return
    discord_mod = types.ModuleType("discord")
    discord_mod.Intents = MagicMock()
    discord_mod.Intents.default.return_value = MagicMock()
    discord_mod.Client = MagicMock
    discord_mod.File = MagicMock
    discord_mod.DMChannel = type("DMChannel", (), {})
    discord_mod.Thread = type("Thread", (), {})
    discord_mod.ForumChannel = type("ForumChannel", (), {})
    discord_mod.MessageType = SimpleNamespace(default=0, reply=1)
    discord_mod.Color = SimpleNamespace(
        blue=lambda: "blue",
        green=lambda: "green",
        red=lambda: "red",
    )
    discord_mod.Interaction = object
    discord_mod.Embed = MagicMock
    discord_mod.ui = SimpleNamespace(
        View=object,
        button=lambda *args, **kwargs: (lambda fn: fn),
        Button=object,
    )
    discord_mod.ButtonStyle = SimpleNamespace(
        success=1,
        primary=2,
        secondary=2,
        danger=3,
        green=1,
        grey=2,
        blurple=2,
        red=3,
    )
    discord_mod.app_commands = SimpleNamespace(
        describe=lambda **kwargs: (lambda fn: fn),
        choices=lambda **kwargs: (lambda fn: fn),
        Choice=lambda **kwargs: SimpleNamespace(**kwargs),
    )

    ext_mod = types.ModuleType("discord.ext")
    commands_mod = types.ModuleType("discord.ext.commands")
    commands_mod.Bot = MagicMock
    ext_mod.commands = commands_mod

    sys.modules.setdefault("discord", discord_mod)
    sys.modules.setdefault("discord.ext", ext_mod)
    sys.modules.setdefault("discord.ext.commands", commands_mod)


_ensure_discord_mock()

import gateway.platforms.discord as discord_platform  # noqa: E402
from gateway.platforms.discord import DiscordAdapter  # noqa: E402


class FakeEmbed:
    def __init__(self, **kwargs):
        self.title = kwargs.get("title")
        self.description = kwargs.get("description")
        self.color = kwargs.get("color")
        self.fields = []

    def add_field(self, *, name, value, inline=False):
        self.fields.append(SimpleNamespace(name=name, value=value, inline=inline))


class FakeDMChannel:
    pass


class FakeTextChannel:
    def __init__(self, channel_id=100, *, topic=None):
        self.id = channel_id
        self.name = "general"
        self.guild = SimpleNamespace(id=5, name="Hermes Server")
        self.topic = topic
        self.edit = AsyncMock(side_effect=self._edit)

    async def _edit(self, **kwargs):
        if "topic" in kwargs:
            self.topic = kwargs["topic"]


class FakeThread:
    def __init__(self, channel_id=200, *, parent=None):
        self.id = channel_id
        self.name = "feature-thread"
        self.parent = parent
        self.parent_id = getattr(parent, "id", None)
        self.guild = getattr(parent, "guild", None)
        self.sent = []

    async def send(self, **kwargs):
        msg = SimpleNamespace(id=300, edit=AsyncMock())
        self.sent.append((kwargs, msg))
        return msg


@pytest.fixture
def adapter(monkeypatch):
    monkeypatch.setattr(discord_platform.discord, "DMChannel", FakeDMChannel, raising=False)
    monkeypatch.setattr(discord_platform.discord, "Thread", FakeThread, raising=False)
    monkeypatch.setattr(discord_platform.discord, "Embed", FakeEmbed, raising=False)
    monkeypatch.setattr(
        discord_platform.discord,
        "Color",
        SimpleNamespace(blue=lambda: "blue", green=lambda: "green", red=lambda: "red"),
        raising=False,
    )
    for var in (
        "DISCORD_REQUIRE_MENTION",
        "DISCORD_AUTO_THREAD",
        "DISCORD_FREE_RESPONSE_CHANNELS",
        "DISCORD_NO_THREAD_CHANNELS",
        "DISCORD_ALLOWED_CHANNELS",
        "DISCORD_IGNORED_CHANNELS",
        "OBSIDIAN_VAULT_PATH",
        "PRODUCTION_URL",
        "NEXT_PUBLIC_SITE_URL",
        "NEXT_PUBLIC_APP_URL",
        "PUBLIC_URL",
        "SITE_URL",
        "APP_URL",
        "DEPLOYMENT_URL",
        "VERCEL_PROJECT_PRODUCTION_URL",
        "VERCEL_URL",
        "HERMES_KANBAN_HOME",
        "HERMES_PUBLIC_KANBAN_BASE_URL",
        "HERMES_DASHBOARD_PUBLIC_URL",
        "HERMES_DASHBOARD_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", "/nonexistent/hermes-test-obsidian-vault")

    instance = DiscordAdapter(PlatformConfig(enabled=True, token="fake-token"))
    instance._client = SimpleNamespace(user=SimpleNamespace(id=999))
    instance._text_batch_delay_seconds = 0
    instance.handle_message = AsyncMock()
    instance._collect_discord_project_metadata = MagicMock(
        return_value={
            "project_name": "Hermes Project",
            "repo_url": "https://github.com/acme/hermes-project",
            "production_url": None,
            "priorities": "pending",
            "branch": "feature/summary",
            "branch_url": "https://github.com/acme/hermes-project/tree/feature/summary",
            "pr_url": None,
        }
    )
    state = {}
    instance._read_project_summary_state = MagicMock(side_effect=lambda: dict(state))
    instance._write_project_summary_state = MagicMock(
        side_effect=lambda value: state.clear() or state.update(value)
    )
    return instance


def _make_message(adapter, *, channel, content):
    return SimpleNamespace(
        id=123,
        content=content,
        mentions=[adapter._client.user],
        attachments=[],
        reference=None,
        created_at=datetime.now(timezone.utc),
        channel=channel,
        author=SimpleNamespace(id=42, display_name="Jezza", name="Jezza"),
        type=discord_platform.discord.MessageType.default,
    )


@pytest.mark.asyncio
async def test_tagged_parent_message_initializes_project_and_feature_summaries(adapter, monkeypatch):
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "true")
    parent = FakeTextChannel(channel_id=100, topic="Existing channel note")
    thread = FakeThread(channel_id=200, parent=parent)
    adapter._auto_create_thread = AsyncMock(return_value=thread)

    await adapter._handle_message(
        _make_message(adapter, channel=parent, content="<@999> Build a deploy dashboard")
    )

    parent.edit.assert_awaited_once()
    assert parent.topic is not None
    assert parent.topic.splitlines() == [
        "\u200b",
        "",
        "pending",
        "https://github.com/acme/hermes-project",
    ]
    assert "Existing channel note" not in parent.topic
    assert len(thread.sent) == 1
    sent_embed = thread.sent[0][0]["embed"]
    assert sent_embed.title == "Generating..."
    assert sent_embed.description is None
    fields = {field.name: field.value for field in sent_embed.fields}
    assert fields["Status"] == "👀 In progress"
    assert all(field.name != "Generated Title" for field in sent_embed.fields)

    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.project_summary["channel_id"] == "100"
    assert event.feature_summary["thread_id"] == "200"
    assert event.text == "Build a deploy dashboard"


@pytest.mark.asyncio
async def test_tagged_thread_followup_reuses_persisted_feature_summary(adapter, monkeypatch):
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "true")
    parent = FakeTextChannel(channel_id=100, topic="Existing channel note")
    thread = FakeThread(channel_id=200, parent=parent)
    handle = await adapter.initialize_feature_summary(
        thread,
        parent_channel=parent,
        initial_request="Build a deploy dashboard",
    )
    assert handle is not None
    adapter.handle_message.reset_mock()

    await adapter._handle_message(
        _make_message(adapter, channel=thread, content="<@999> Also add export")
    )

    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.source.chat_type == "thread"
    assert event.feature_summary["thread_id"] == "200"
    assert event.feature_summary["message_id"] == "300"
    assert event.feature_summary["_thread_obj"] is thread


@pytest.mark.asyncio
async def test_project_channel_mapping_reaches_event_and_summary_handles(adapter, monkeypatch):
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "true")
    parent = FakeTextChannel(channel_id=100, topic="Existing channel note")
    thread = FakeThread(channel_id=200, parent=parent)
    adapter._auto_create_thread = AsyncMock(return_value=thread)
    project_context = {
        "project_channel_id": "100",
        "project_name": "PID",
        "project_path": "/home/droid/.hermes/workspace/PID",
        "project_github_url": "https://github.com/sligo-labs/pid",
        "project_mapping_source": "manual",
        "project_mapping_resolved": True,
    }
    monkeypatch.setattr(
        discord_platform,
        "resolve_discord_project_context",
        lambda channel: SimpleNamespace(to_dict=lambda: dict(project_context)),
    )

    await adapter._handle_message(
        _make_message(adapter, channel=parent, content="<@999> Build project telemetry")
    )

    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.source.project_channel_id == "100"
    assert event.source.project_name == "PID"
    assert event.source.project_path == "/home/droid/.hermes/workspace/PID"
    assert event.source.project_github_url == "https://github.com/sligo-labs/pid"
    assert event.project_summary["project_context"] == {
        **project_context,
        "channel_name": "general",
    }
    assert event.feature_summary["project_context"] == project_context


def test_project_description_contract_contains_static_project_access(adapter):
    topic = adapter._render_project_summary_line(
        {
            "production_url": "https://pid.sligo-labs.vercel.app",
            "repo_url": "https://github.com/sligo-labs/PID",
            "priorities": "Do not put state here",
            "app_access": "username admin / password PID-2026",
        }
    )

    assert topic.splitlines() == [
        "\u200b",
        "",
        "https://pid.sligo-labs.vercel.app/",
        "https://github.com/sligo-labs/PID",
        "username: admin",
        "password: PID-2026",
    ]
    assert "Project Summary:" not in topic
    assert "Production URL:" not in topic
    assert "GitHub URL:" not in topic
    assert "Next priorities:" not in topic
    assert "Do not put state here" not in topic
    assert len(topic) <= 1024


def test_project_description_omits_unparseable_app_access(adapter):
    topic = adapter._render_project_summary_line(
        {
            "production_url": "https://pid.sligo-labs.vercel.app",
            "repo_url": "https://github.com/sligo-labs/PID",
            "app_access": "use the shared demo account from the project note",
        }
    )

    assert topic.splitlines() == [
        "\u200b",
        "",
        "https://pid.sligo-labs.vercel.app/",
        "https://github.com/sligo-labs/PID",
    ]
    assert "username:" not in topic
    assert "password:" not in topic


def test_project_description_parses_semicolon_credentials(adapter):
    topic = adapter._render_project_summary_line(
        {
            "production_url": "https://app.example.com",
            "repo_url": "https://github.com/acme/app",
            "app_access": "username demo; password: secret-value",
        }
    )

    assert topic.splitlines() == [
        "\u200b",
        "",
        "https://app.example.com/",
        "https://github.com/acme/app",
        "username: demo",
        "password: secret-value",
    ]


def test_project_priorities_support_implementation_priority_pseudo_heading(adapter):
    priorities = adapter._extract_obsidian_priorities(
        """## Current Client Focus

Implementation priority:

1. Build Arizona SOS ingestion.
2. Verify Maricopa registration ingestion.

Scraper design rule: keep it small.
"""
    )

    assert priorities == "Build Arizona SOS ingestion.; Verify Maricopa registration ingestion."


def test_project_metadata_reads_obsidian_frontmatter_app_access(adapter, tmp_path, monkeypatch):
    adapter._collect_discord_project_metadata = (
        DiscordAdapter._collect_discord_project_metadata.__get__(adapter, DiscordAdapter)
    )
    vault = tmp_path / "vault"
    projects = vault / "Projects"
    projects.mkdir(parents=True)
    (projects / "PID.md").write_text(
        """---
app_login: use the shared demo account from the project note
credentials: username demo; password: secret-value
login_required: yes
---
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(vault))
    adapter._summary_workdir = MagicMock(side_effect=AssertionError("should not use gateway cwd"))

    metadata = adapter._collect_discord_project_metadata(
        {
            "project_name": "PID",
            "project_path": "/missing/project/path",
            "project_mapping_resolved": True,
        }
    )

    assert metadata["app_access"] == (
        "use the shared demo account from the project note; "
        "username demo; password: secret-value"
    )


def test_project_metadata_does_not_fall_back_to_cwd_when_mapped_path_missing(adapter):
    adapter._collect_discord_project_metadata = (
        DiscordAdapter._collect_discord_project_metadata.__get__(adapter, DiscordAdapter)
    )
    adapter._summary_workdir = MagicMock(side_effect=AssertionError("should not use gateway cwd"))

    metadata = adapter._collect_discord_project_metadata(
        {
            "project_name": "PID",
            "project_path": "/missing/project/path",
            "project_github_url": "git@github.com:sligo-labs/pid.git",
            "project_mapping_resolved": True,
        }
    )

    assert metadata["project_name"] == "PID"
    assert metadata["repo_url"] == "https://github.com/sligo-labs/pid"
    assert metadata["branch"] is None


@pytest.mark.asyncio
async def test_project_description_is_not_refreshed_after_previous_success(adapter):
    parent = FakeTextChannel(channel_id=100, topic="Existing channel note")
    key = adapter._project_summary_state_key(parent)
    state = {
        key: {
            "channel_id": "100",
            "guild_id": "5",
            "attempted_at": 1,
            "success": True,
        }
    }
    adapter._read_project_summary_state = MagicMock(side_effect=lambda: dict(state))
    adapter._write_project_summary_state = MagicMock(
        side_effect=lambda value: state.clear() or state.update(value)
    )
    adapter._collect_discord_project_metadata = MagicMock(
        return_value={
            "production_url": "https://new.example.com",
            "repo_url": "https://github.com/acme/new",
            "priorities": "Refresh from Obsidian",
        }
    )

    handle = await adapter.initialize_project_summary(parent)

    parent.edit.assert_not_awaited()
    adapter._write_project_summary_state.assert_not_called()
    adapter._collect_discord_project_metadata.assert_not_called()
    assert handle is None
    assert parent.topic == "Existing channel note"
    assert state[key]["success"] is True


@pytest.mark.asyncio
async def test_project_summary_retries_after_failed_attempt(adapter):
    parent = FakeTextChannel(channel_id=100, topic=None)
    key = adapter._project_summary_state_key(parent)
    state = {
        key: {
            "channel_id": "100",
            "guild_id": "5",
            "attempted_at": 1,
            "success": False,
        }
    }
    adapter._read_project_summary_state = MagicMock(side_effect=lambda: dict(state))
    adapter._write_project_summary_state = MagicMock(side_effect=lambda value: state.clear() or state.update(value))

    handle = await adapter.initialize_project_summary(parent)

    parent.edit.assert_awaited_once()
    assert handle is not None
    assert handle["channel_id"] == "100"
    assert state[key]["success"] is True


@pytest.mark.asyncio
async def test_feature_summary_update_edits_initial_message(adapter):
    parent = FakeTextChannel(channel_id=100)
    thread = FakeThread(channel_id=200, parent=parent)
    handle = await adapter.initialize_feature_summary(
        thread,
        parent_channel=parent,
        initial_request="Ship project links",
    )

    assert handle is not None
    assert await adapter.update_feature_summary(
        handle,
        final_response="Done. The repo and production links are visible.",
        status="Complete",
        title="Project Links",
    )

    message = handle["_message_obj"]
    message.edit.assert_awaited_once()
    edited_embed = message.edit.await_args.kwargs["embed"]
    fields = {field.name: field.value for field in edited_embed.fields}
    assert edited_embed.title == "Project Links"
    assert edited_embed.description is None
    assert "Generated Title" not in fields
    assert fields["Status"] == "✅ Done"
    assert fields["Concise Outcome"].startswith("Done.")
    assert fields["Branch"] == "feature/summary"


@pytest.mark.asyncio
async def test_feature_summary_uses_absolute_kanban_board_url(adapter, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    monkeypatch.setenv("HERMES_PUBLIC_KANBAN_BASE_URL", "https://hermes.sligolabs.com")
    parent = FakeTextChannel(channel_id=100)
    thread = FakeThread(channel_id=200, parent=parent)

    handle = await adapter.initialize_feature_summary(
        thread,
        parent_channel=parent,
        initial_request="Ship project links",
    )

    assert handle is not None
    sent_embed = thread.sent[0][0]["embed"]
    fields = {field.name: field.value for field in sent_embed.fields}
    assert fields["Kanban Board"] == "https://hermes.sligolabs.com/workers/200"


@pytest.mark.asyncio
async def test_feature_summary_omits_relative_kanban_board_path(adapter, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    monkeypatch.setenv("PUBLIC_URL", "https://pid.sligo-labs.vercel.app")
    parent = FakeTextChannel(channel_id=100)
    thread = FakeThread(channel_id=200, parent=parent)

    handle = await adapter.initialize_feature_summary(
        thread,
        parent_channel=parent,
        initial_request="Ship project links",
    )

    assert handle is not None
    sent_embed = thread.sent[0][0]["embed"]
    fields = {field.name: field.value for field in sent_embed.fields}
    assert "Kanban Board" not in fields


@pytest.mark.asyncio
async def test_tagged_question_answers_in_place_without_feature_summary(adapter, monkeypatch):
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "true")
    parent = FakeTextChannel(channel_id=100, topic="Existing channel note")
    adapter._auto_create_thread = AsyncMock()

    await adapter._handle_message(
        _make_message(adapter, channel=parent, content="<@999> What is the repo URL?")
    )

    parent.edit.assert_not_awaited()
    adapter._auto_create_thread.assert_not_awaited()
    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.feature_summary is None
    assert event.project_summary is None
    assert event.source.chat_id == "100"
    assert event.source.chat_type == "group"
    assert "classified as a direct question/request" in event.channel_prompt


@pytest.mark.asyncio
async def test_tagged_priority_change_does_not_refresh_project_description(adapter, monkeypatch):
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "true")
    parent = FakeTextChannel(channel_id=100, topic="Existing channel note")
    adapter._auto_create_thread = AsyncMock()
    adapter._classify_discord_feature_request = AsyncMock(return_value=False)

    await adapter._handle_message(
        _make_message(adapter, channel=parent, content="<@999> Change the next priorities to scraper validation")
    )

    parent.edit.assert_not_awaited()
    adapter._auto_create_thread.assert_not_awaited()
    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.feature_summary is None
    assert event.project_summary is None
    assert "classified as a direct question/request" in event.channel_prompt


@pytest.mark.asyncio
async def test_tagged_thread_question_gets_direct_answer_prompt(adapter, monkeypatch):
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "true")
    parent = FakeTextChannel(channel_id=100)
    thread = FakeThread(channel_id=200, parent=parent)
    adapter._auto_create_thread = AsyncMock()

    await adapter._handle_message(
        _make_message(adapter, channel=thread, content="<@999> What changed?")
    )

    adapter._auto_create_thread.assert_not_awaited()
    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.feature_summary is None
    assert event.source.chat_id == "200"
    assert event.source.chat_type == "thread"
    assert "classified as a direct question/request" in event.channel_prompt


@pytest.mark.asyncio
async def test_native_voice_feature_request_triages_from_transcript(adapter, monkeypatch):
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "true")
    parent = FakeTextChannel(channel_id=100, topic="Existing channel note")
    thread = FakeThread(channel_id=200, parent=parent)
    adapter._auto_create_thread = AsyncMock(return_value=thread)
    att = SimpleNamespace(
        url="https://cdn.discordapp.com/attachments/fake/voice-message.ogg",
        filename="voice-message.ogg",
        content_type=None,
        size=10,
        read=AsyncMock(return_value=b"fake ogg"),
        duration_secs=2.0,
        waveform=b"fake",
    )
    message = SimpleNamespace(
        id=123,
        content="",
        mentions=[],
        attachments=[att],
        reference=None,
        created_at=datetime.now(timezone.utc),
        channel=parent,
        guild=parent.guild,
        author=SimpleNamespace(id=42, display_name="Jezza", name="Jezza", bot=False),
        flags=SimpleNamespace(voice=True),
        type=discord_platform.discord.MessageType.default,
    )

    with patch(
        "gateway.platforms.discord.cache_audio_from_bytes",
        return_value="/tmp/voice_from_read.ogg",
    ) as mock_cache, patch(
        "tools.transcription_tools.transcribe_audio",
        return_value={"success": True, "transcript": "Build a deploy dashboard"},
    ) as mock_transcribe:
        await adapter._handle_message(message)

    mock_cache.assert_called_once_with(b"fake ogg", ext=".ogg")
    mock_transcribe.assert_called_once_with("/tmp/voice_from_read.ogg")
    adapter._auto_create_thread.assert_awaited_once_with(message)
    assert len(thread.sent) == 1
    event = adapter.handle_message.await_args.args[0]
    assert event.feature_summary["thread_id"] == "200"
    assert event.media_urls == ["/tmp/voice_from_read.ogg"]
    assert event.media_types == ["audio/ogg"]


def test_failed_feature_summary_status_uses_red_cross(adapter):
    embed = adapter._build_feature_summary_embed(
        initial_request="",
        status="Failed",
        outcome="The run failed.",
        title="Project Links",
    )
    fields = {field.name: field.value for field in embed.fields}
    assert fields["Status"] == "❌ Failed"
    assert embed.color == "red"


def test_runner_summarizes_long_discord_feature_outcome():
    runner = object.__new__(gateway_run.GatewayRunner)
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content="Built the dashboard and added focused regression coverage. The branch is ready for review."
                )
            )
        ]
    )
    long_response = "Done. " + "Implemented details. " * 80

    with patch("agent.auxiliary_client.call_llm", return_value=response) as call_llm:
        summary = runner._summarize_discord_feature_outcome(long_response)

    assert summary == "Built the dashboard and added focused regression coverage. The branch is ready for review."
    call_llm.assert_called_once()
    prompt = call_llm.call_args.kwargs["messages"][1]["content"]
    assert "few concise sentences" in prompt


@pytest.mark.asyncio
async def test_runner_registers_discord_summary_post_delivery_callback():
    runner = object.__new__(gateway_run.GatewayRunner)
    runner._session_db = None
    adapter = SimpleNamespace(
        callbacks=[],
        update_project_summary=AsyncMock(),
        update_feature_summary=AsyncMock(),
    )

    def register_post_delivery_callback(session_key, callback, generation=None):
        adapter.callbacks.append((session_key, callback, generation))

    adapter.register_post_delivery_callback = register_post_delivery_callback
    runner.adapters = {Platform.DISCORD: adapter}
    source = SessionSource(platform=Platform.DISCORD, chat_id="200", chat_type="thread")
    event = MessageEvent(
        text="Build it",
        source=source,
        feature_summary={"message_id": "300"},
        project_summary={"channel_id": "100"},
    )

    runner._register_discord_summary_post_delivery(
        event=event,
        source=source,
        session_key="discord:200",
        run_generation=7,
        session_id="session-1",
        final_response="Final answer",
        agent_result={"completed": True},
    )

    assert len(adapter.callbacks) == 1
    session_key, callback, generation = adapter.callbacks[0]
    assert session_key == "discord:200"
    assert generation == 7
    result = callback()
    if inspect.isawaitable(result):
        await result

    adapter.update_project_summary.assert_not_awaited()
    adapter.update_feature_summary.assert_awaited_once_with(
        {"message_id": "300"},
        final_response="Final answer",
        status="Complete",
        title=None,
    )
