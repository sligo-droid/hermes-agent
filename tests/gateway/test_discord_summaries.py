"""Tests for Discord project and feature summary surfaces."""

import inspect
import sys
import types
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType, ProcessingOutcome
from gateway.session import SessionSource
from gateway.work_ledger import GatewayWorkLedger
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
    discord_mod.NotFound = type("NotFound", (Exception,), {})
    discord_mod.Forbidden = type("Forbidden", (Exception,), {})
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

import plugins.platforms.discord.adapter as discord_platform  # noqa: E402
from plugins.platforms.discord.adapter import DiscordAdapter  # noqa: E402


DISCORD_EPOCH_SECONDS = 1_420_070_400.0


def _discord_snowflake_at(timestamp: float) -> str:
    return str(int((timestamp - DISCORD_EPOCH_SECONDS) * 1000) << 22)


class FakeEmbed:
    def __init__(self, **kwargs):
        self.title = kwargs.get("title")
        self.description = kwargs.get("description")
        self.color = kwargs.get("color")
        self.fields = []

    def add_field(self, *, name, value, inline=False):
        self.fields.append(SimpleNamespace(name=name, value=value, inline=inline))

    def to_dict(self):
        payload = {
            "title": self.title,
            "description": self.description,
            "fields": [
                {"name": field.name, "value": field.value, "inline": field.inline}
                for field in self.fields
            ],
        }
        if self.color is not None:
            payload["color"] = self.color
        return payload


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
        self._messages = {}

    async def send(self, **kwargs):
        msg = SimpleNamespace(
            id=300 + len(self.sent),
            edit=AsyncMock(),
            add_reaction=AsyncMock(),
            remove_reaction=AsyncMock(),
            reactions=[],
        )
        self.sent.append((kwargs, msg))
        self._messages[msg.id] = msg
        return msg

    async def fetch_message(self, message_id):
        return self._messages[int(message_id)]


class FakeAttachment:
    def __init__(self, *, filename, content_type, data):
        self.filename = filename
        self.content_type = content_type
        self.size = len(data)
        self.url = "https://cdn.discordapp.com/attachments/fake/file"
        self._data = data

    async def read(self):
        return self._data


@pytest.fixture
def adapter(monkeypatch):
    monkeypatch.setattr(discord_platform.discord, "DMChannel", FakeDMChannel, raising=False)
    monkeypatch.setattr(discord_platform.discord, "Thread", FakeThread, raising=False)
    monkeypatch.setattr(discord_platform.discord, "Embed", FakeEmbed, raising=False)
    monkeypatch.setattr(discord_platform.discord, "NotFound", type("NotFound", (Exception,), {}), raising=False)
    monkeypatch.setattr(discord_platform.discord, "Forbidden", type("Forbidden", (Exception,), {}), raising=False)
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


def _make_message(adapter, *, channel, content, message_id=123, attachments=None):
    return SimpleNamespace(
        id=message_id,
        content=content,
        mentions=[adapter._client.user],
        attachments=attachments or [],
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
    assert fields["Status"] == "⏳ In progress"
    assert fields["Branch"] == "[feature/summary](https://github.com/acme/hermes-project/tree/feature/summary)"
    assert "Feature Branch URL" not in fields
    assert all(field.name != "Generated Title" for field in sent_embed.fields)

    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.project_summary["channel_id"] == "100"
    assert event.feature_summary["thread_id"] == "200"
    assert event.feature_summary["kanban_board"] is None
    assert event.feature_summary["initial_request"] == "Build a deploy dashboard"
    tier = gateway_run._discord_action_request_model_tier({}, event.feature_summary)
    assert tier is not None
    assert (tier.name, tier.model, tier.reasoning_effort) == (
        "discord_action",
        "gpt-5.6-sol",
        "medium",
    )
    assert event.text == "Build a deploy dashboard"
    assert event.message_type == MessageType.TEXT


@pytest.mark.asyncio
async def test_ack_prefixed_run_pipeline_request_gets_feature_summary(adapter, monkeypatch):
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "true")
    parent = FakeTextChannel(channel_id=100, topic="Existing channel note")
    thread = FakeThread(channel_id=200, parent=parent)
    adapter._auto_create_thread = AsyncMock(return_value=thread)

    await adapter._handle_message(
        _make_message(
            adapter,
            channel=parent,
            content=(
                "<@999> okay the pangram API key billing has been resolved, "
                "run the entire pipeline for 2024-time-restricted-eating from scratch"
            ),
        )
    )

    assert len(thread.sent) == 1
    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.channel_prompt is None
    assert event.feature_summary is not None
    assert event.feature_summary["thread_id"] == "200"
    assert event.text.startswith("okay the pangram API key billing has been resolved")


@pytest.mark.asyncio
async def test_narrative_prefixed_review_request_runs_read_only_directly(
    adapter,
    monkeypatch,
):
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "false")
    parent = FakeTextChannel(channel_id=100, topic="Existing channel note")
    thread = FakeThread(channel_id=200, parent=parent)
    adapter._auto_create_thread = AsyncMock(return_value=thread)
    request = (
        "we tried to implement some stuff yesterday via discord but it was a bit "
        "of a disaster. look through the site the way a human would and try to "
        "identify things you may have broken. also look through the commits for "
        "areas you changed and focus on those. produce a list of recommendations"
    )

    await adapter._handle_message(
        _make_message(adapter, channel=parent, content=request)
    )

    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.discord_runtime_mode == "read_only"
    assert "default READ-ONLY runtime" in event.channel_prompt
    assert event.feature_summary is None
    assert event.participates_in_work_lifecycle is False
    assert event.discord_action_escalation_allowed is True
    assert event.discord_runtime_reason == "classified_read_only"
    assert event.source.chat_id == "200"
    adapter._auto_create_thread.assert_awaited_once()


@pytest.mark.asyncio
async def test_discord_message_link_routes_direct_action_without_mutation_verb(adapter, monkeypatch):
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "false")
    parent = FakeTextChannel(channel_id=100, topic="Existing channel note")
    thread = FakeThread(channel_id=200, parent=parent)
    adapter._auto_create_thread = AsyncMock(return_value=thread)

    await adapter._handle_message(
        _make_message(
            adapter,
            channel=parent,
            content="https://discord.com/channels/1/2/3",
        )
    )

    event = adapter.handle_message.await_args.args[0]
    assert event.discord_runtime_mode == "action"
    assert event.feature_summary is not None
    assert event.participates_in_work_lifecycle is True


@pytest.mark.asyncio
async def test_discord_message_link_explicit_plan_only_stays_read_only(adapter, monkeypatch):
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "false")
    parent = FakeTextChannel(channel_id=100, topic="Existing channel note")
    thread = FakeThread(channel_id=200, parent=parent)
    adapter._auto_create_thread = AsyncMock(return_value=thread)

    await adapter._handle_message(
        _make_message(
            adapter,
            channel=parent,
            content=(
                "Plan only; do not implement changes for "
                "https://discord.com/channels/1/2/3"
            ),
        )
    )

    event = adapter.handle_message.await_args.args[0]
    assert event.discord_runtime_mode == "read_only"
    assert event.discord_action_escalation_allowed is True
    assert event.feature_summary is None


@pytest.mark.asyncio
async def test_non_goal_feature_summary_does_not_start_kanban_pipeline(adapter, monkeypatch):
    from hermes_cli import discord_worker_boards as dwb

    def fail_start_planner_request(**kwargs):
        raise AssertionError("non-/goal feature summaries must not start Kanban planner work")

    monkeypatch.setattr(dwb, "start_planner_request", fail_start_planner_request)
    parent = FakeTextChannel(channel_id=100, topic="Existing channel note")
    thread = FakeThread(channel_id=200, parent=parent)

    handle = await adapter.initialize_feature_summary(
        thread,
        parent_channel=parent,
        initial_request="Build a deploy dashboard",
    )

    assert handle is not None
    assert handle["kanban_board"] is None
    fields = {field.name: field.value for field in thread.sent[0][0]["embed"].fields}
    assert "Kanban Board" not in fields


@pytest.mark.asyncio
async def test_high_confidence_operational_request_routes_action_without_auxiliary_llm(adapter, monkeypatch):
    monkeypatch.setattr(
        "agent.auxiliary_client.call_llm",
        MagicMock(side_effect=AssertionError("intake must not call auxiliary LLM")),
    )

    assert await adapter._classify_discord_runtime_mode(
        "The deploy failed; explain why and rerun it",
        context_lines=["Alex: this may need investigation"],
    ) is gateway_run.RuntimeMode.ACTION


@pytest.mark.parametrize(
    "request_text",
    [
        "Audit the production permission model and report findings.",
        "Research how the provider handles retries.",
        "Verify the rendered route and tell me the next fixes.",
        "Plan the migration and recommend the safest sequence.",
        (
            "confirm verification / rendered route QA for the current change "
            "and tell me what would be the next fixes"
        ),
    ],
)
@pytest.mark.asyncio
async def test_observational_discord_tasks_route_read_only(adapter, request_text):
    assert await adapter._classify_discord_runtime_mode(request_text) is gateway_run.RuntimeMode.READ_ONLY


@pytest.mark.asyncio
async def test_discord_runtime_mode_distinguishes_mutation_ambiguity_and_thread_constraints(adapter):
    assert await adapter._classify_discord_runtime_mode(
        "Implement the parser fix and ship it."
    ) is gateway_run.RuntimeMode.ACTION
    assert await adapter._classify_discord_runtime_mode(
        "Could this parser be improved?"
    ) is gateway_run.RuntimeMode.READ_ONLY
    assert await adapter._classify_discord_runtime_mode(
        "Okay, let's build this.",
        actionable_thread_context=True,
    ) is gateway_run.RuntimeMode.ACTION
    for request in (
        "Do not implement; for now just plan the fix.",
        "Recommend only, no changes.",
        "Tell me what you would do to fix it.",
        "Do not take any action; analyze it only.",
        "Read-only review: don't actually make changes.",
    ):
        assert await adapter._classify_discord_runtime_mode(
            request,
            actionable_thread_context=True,
            force_action=True,
        ) is gateway_run.RuntimeMode.READ_ONLY


def test_read_only_prompt_lightly_prefers_direct_work_without_restricting_delegation(adapter):
    prompt = adapter._append_direct_question_prompt("")

    assert "small, tightly coupled observations" in prompt
    assert "working directly is often faster" in prompt
    assert "parallelism, independent verification, context isolation, or deeper reasoning" in prompt
    assert "read-only delegation remains available" in prompt
    assert "any configured tier that matches the subtask's difficulty" in prompt
    assert "read-only does not limit tier choice" in prompt
    assert "never escalate merely to gain tool access" in prompt


@pytest.mark.parametrize(
    "request_text",
    [
        "Please fix the parser.",
        "Could you please build the deployment dashboard.",
        "Please implement the approved change.",
        "Please deploy the current branch.",
        "Please update the retry policy.",
        "Please implement read-only parser mode.",
        "Review the local district map panel and repair its layout.",
    ],
)
@pytest.mark.asyncio
async def test_polite_mutation_requests_route_directly_to_action(adapter, request_text):
    assert await adapter._classify_discord_runtime_mode(request_text) is gateway_run.RuntimeMode.ACTION


@pytest.mark.asyncio
async def test_discord_message_links_keep_action_fast_path_but_no_action_wins(adapter):
    link = "https://discord.com/channels/1/2/3"
    assert await adapter._classify_discord_runtime_mode(
        f"Investigate and fix {link}",
        force_action=True,
    ) is gateway_run.RuntimeMode.ACTION
    assert await adapter._classify_discord_runtime_mode(
        f"Plan only; do not implement changes for {link}",
        force_action=True,
    ) is gateway_run.RuntimeMode.READ_ONLY


def test_classifier_authority_keeps_escalation_available_on_read_only_turns(adapter):
    reason, allowed = adapter._discord_runtime_authority(
        "Tell me what you would do to fix it.",
        gateway_run.RuntimeMode.READ_ONLY,
        force_action=True,
    )
    assert reason == "hypothetical_action_only"
    assert allowed is True

    reason, allowed = adapter._discord_runtime_authority(
        "Could this be improved?",
        gateway_run.RuntimeMode.READ_ONLY,
    )
    assert reason == "classified_read_only"
    assert allowed is True

    later_reason, later_allowed = adapter._discord_runtime_authority(
        "Please implement the parser fix.",
        gateway_run.RuntimeMode.ACTION,
    )
    assert later_reason == "explicit_action_request"
    assert later_allowed is False


@pytest.mark.asyncio
async def test_promote_existing_question_thread_initializes_action_event(adapter):
    parent = FakeTextChannel(channel_id=100)
    thread = FakeThread(channel_id=200, parent=parent)
    raw = _make_message(adapter, channel=thread, content="Could you make this work?")
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="200",
        chat_type="thread",
        thread_id="200",
        parent_chat_id="100",
        guild_id="5",
        message_id="123",
    )
    event = MessageEvent(
        text="Could you make this work?",
        source=source,
        raw_message=raw,
        message_id="123",
        media_urls=["/tmp/reference.png"],
        media_types=["image/png"],
        reply_to_message_id="reply-1",
        reply_to_text="Earlier context",
        channel_prompt="question overlay",
        channel_context="[Recent channel messages]\nprior context",
        goal_thread_context="[Goal thread context]\nprior goal context",
        discord_action_request_base_channel_prompt="base prompt",
        discord_runtime_mode="read_only",
        discord_action_escalation_allowed=True,
        metadata={
            "discord_request_ts": 1.0,
            "discord_adapter_dispatch_ts": 2.0,
            "gateway_intake_ts": 3.0,
            "gateway_admitted_ts": 4.0,
            "gateway_agent_handler_start_ts": 5.0,
            "gateway_flow_phase_timestamps": {"request_ts": 1.0},
            "gateway_flow_phase_durations": {"ledger_claim_ms": 7},
            "preserved": "yes",
        },
    )
    adapter._resolve_channel_by_id = AsyncMock(return_value=thread)
    adapter._resolve_project_context_for_channel = MagicMock(return_value=None)
    adapter._load_feature_summary_handle_for_request = MagicMock(return_value=None)
    feature_summary = {
        "thread_id": "200",
        "message_id": "300",
        "initial_request": event.text,
        "kanban_board": None,
    }
    adapter.initialize_feature_summary = AsyncMock(return_value=feature_summary)
    adapter.initialize_project_summary = AsyncMock(return_value=None)
    adapter._threads.mark = MagicMock()
    adapter._mark_discord_thread_participation = MagicMock()

    promoted, url = await adapter.promote_event_to_action_request(
        event,
        initial_request=event.text,
    )

    assert promoted is not None
    assert promoted.source.chat_id == "200"
    assert promoted.discord_runtime_mode == "action"
    assert promoted.discord_action_request_intent is None
    assert promoted.discord_action_escalation_allowed is False
    assert promoted.discord_runtime_reason == "promoted_action_replay"
    assert promoted.channel_prompt == "base prompt"
    assert promoted.feature_summary is feature_summary
    assert promoted.internal is False
    assert promoted.participates_in_work_lifecycle is True
    assert promoted.media_urls == ["/tmp/reference.png"]
    assert promoted.media_types == ["image/png"]
    assert promoted.reply_to_message_id == "reply-1"
    assert promoted.reply_to_text == "Earlier context"
    assert promoted.channel_context == "[Recent channel messages]\nprior context"
    assert promoted.goal_thread_context == "[Goal thread context]\nprior goal context"
    assert promoted.metadata is not event.metadata
    assert promoted.metadata["preserved"] == "yes"
    assert promoted.metadata["discord_promotion_handoff_ts"] > 0
    for key in (
        "discord_request_ts",
        "discord_adapter_dispatch_ts",
        "gateway_intake_ts",
        "gateway_admitted_ts",
        "gateway_agent_handler_start_ts",
        "gateway_flow_phase_timestamps",
        "gateway_flow_phase_durations",
    ):
        assert key not in promoted.metadata
    assert event.metadata["discord_request_ts"] == 1.0
    assert url == "https://discord.com/channels/5/200"


@pytest.mark.asyncio
async def test_promote_action_replay_reuses_summary_for_same_source_message(adapter):
    parent = FakeTextChannel(channel_id=100)
    thread = FakeThread(channel_id=200, parent=parent)
    raw = _make_message(adapter, channel=thread, content="Could you make this work?", message_id=123)
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="200",
        chat_type="thread",
        thread_id="200",
        parent_chat_id="100",
        guild_id="5",
        message_id="123",
    )
    event = MessageEvent(
        text="Could you make this work?",
        source=source,
        raw_message=raw,
        message_id="123",
        discord_action_request_intent=False,
        discord_action_escalation_allowed=True,
    )
    feature_summary = {
        "thread_id": "200",
        "message_id": "300",
        "source_message_id": "123",
        "initial_request": event.text,
        "kanban_board": None,
    }
    adapter._resolve_channel_by_id = AsyncMock(return_value=thread)
    adapter._resolve_project_context_for_channel = MagicMock(return_value=None)
    adapter._load_feature_summary_handle_for_request = MagicMock(return_value=feature_summary)
    adapter.initialize_feature_summary = AsyncMock()
    adapter.initialize_project_summary = AsyncMock(return_value=None)

    promoted, _url = await adapter.promote_event_to_action_request(
        event,
        initial_request=event.text,
    )

    assert promoted is not None
    assert promoted.feature_summary is feature_summary
    adapter._load_feature_summary_handle_for_request.assert_called_once_with(
        thread,
        source_message_id="123",
        project_context=None,
    )
    adapter.initialize_feature_summary.assert_not_awaited()


@pytest.mark.asyncio
async def test_promote_inline_parent_intake_creates_action_thread(adapter):
    parent = FakeTextChannel(channel_id=100)
    thread = FakeThread(channel_id=200, parent=parent)
    raw = _make_message(adapter, channel=parent, content="Should we rerun the deploy?")
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="100",
        chat_type="group",
        guild_id="5",
        message_id="123",
    )
    event = MessageEvent(
        text="Should we rerun the deploy?",
        source=source,
        raw_message=raw,
        message_id="123",
        discord_action_request_intent=False,
        discord_action_escalation_allowed=True,
    )
    adapter._auto_create_thread = AsyncMock(return_value=thread)
    adapter._resolve_project_context_for_channel = MagicMock(return_value=None)
    adapter._load_feature_summary_handle_for_request = MagicMock(return_value=None)
    feature_summary = {
        "thread_id": "200",
        "message_id": "300",
        "initial_request": event.text,
        "kanban_board": None,
    }
    adapter.initialize_feature_summary = AsyncMock(return_value=feature_summary)
    adapter.initialize_project_summary = AsyncMock(return_value=None)
    adapter._threads.mark = MagicMock()
    adapter._mark_discord_thread_participation = MagicMock()

    promoted, _url = await adapter.promote_event_to_action_request(
        event,
        initial_request=event.text,
    )

    adapter._auto_create_thread.assert_awaited_once_with(
        raw,
        generation_is_current=None,
    )
    assert promoted is not None
    assert promoted.source.chat_id == "200"
    assert promoted.source.parent_chat_id == "100"
    assert promoted.source.auto_thread_created is True
    assert promoted.feature_summary is feature_summary


@pytest.mark.asyncio
async def test_action_promotion_generation_barrier_rolls_back_new_thread(adapter):
    parent = FakeTextChannel(channel_id=100)
    thread = FakeThread(channel_id=200, parent=parent)
    thread.delete = AsyncMock()
    raw = _make_message(adapter, channel=parent, content="Could you make this work?")
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="100",
        chat_type="group",
        guild_id="5",
        message_id="123",
    )
    event = MessageEvent(
        text="Could you make this work?",
        source=source,
        raw_message=raw,
        message_id="123",
        discord_runtime_mode="read_only",
        discord_action_escalation_allowed=True,
    )
    current = {"value": True}

    async def create_thread(_raw, *, generation_is_current=None):
        assert generation_is_current is not None
        current["value"] = False
        return thread

    adapter._auto_create_thread = AsyncMock(side_effect=create_thread)
    adapter.initialize_feature_summary = AsyncMock()

    promoted, url = await adapter.promote_event_to_action_request(
        event,
        initial_request=event.text,
        generation_is_current=lambda: current["value"],
    )

    assert promoted is None
    assert url == ""
    thread.delete.assert_awaited_once()
    adapter.initialize_feature_summary.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_goal_feature_summary_can_render_explicit_pr_url(adapter):
    parent = FakeTextChannel(channel_id=100, topic="Existing channel note")
    thread = FakeThread(channel_id=200, parent=parent)
    pr_url = "https://github.com/acme/PID/pull/42"

    handle = await adapter.initialize_feature_summary(
        thread,
        parent_channel=parent,
        initial_request="Cron job shipped a change",
    )
    assert handle is not None
    handle["pr_url"] = pr_url

    assert await adapter.update_feature_summary(
        handle,
        final_response="Opened a PID UX bugfix PR.",
        status="Complete",
        title="Daily PID admin dogfood UX bugfix PR #42",
    )

    message = handle["_message_obj"]
    edited_embed = message.edit.await_args.kwargs["embed"]
    fields = {field.name: field.value for field in edited_embed.fields}
    assert fields["GitHub PR"] == pr_url
    assert "Kanban Board" not in fields


@pytest.mark.asyncio
async def test_goal_feature_summary_keeps_worker_board_handle(adapter):
    parent = FakeTextChannel(channel_id=100, topic="Existing channel note")
    thread = FakeThread(channel_id=200, parent=parent)

    handle = await adapter.initialize_feature_summary(
        thread,
        parent_channel=parent,
        initial_request="/goal Build a deploy dashboard",
    )

    assert handle is not None
    assert handle["kanban_board"]["slug"] == "discord-200"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("initial_request", "expects_board"),
    [
        ("Build a deploy dashboard", False),
        ("/goal Build a deploy dashboard", True),
    ],
)
async def test_feature_summary_adds_status_reaction_to_embed_message(
    adapter,
    monkeypatch,
    tmp_path,
    initial_request,
    expects_board,
):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    parent = FakeTextChannel(channel_id=100, topic="Existing channel note")
    thread = FakeThread(channel_id=200, parent=parent)

    handle = await adapter.initialize_feature_summary(
        thread,
        parent_channel=parent,
        initial_request=initial_request,
    )

    assert handle is not None
    assert bool(handle["kanban_board"]) is expects_board
    message = handle["_message_obj"]
    message.add_reaction.assert_awaited_once_with("⏳")


@pytest.mark.asyncio
async def test_goal_feature_summary_for_source_uses_standard_goal_embed(adapter, monkeypatch):
    monkeypatch.setenv("HERMES_PUBLIC_KANBAN_BASE_URL", "https://kanban.example")
    parent = FakeTextChannel(channel_id=100, topic="Existing channel note")
    thread = FakeThread(channel_id=200, parent=parent)

    async def resolve_channel(channel_id):
        return {"100": parent, "200": thread}.get(str(channel_id))

    adapter._resolve_channel_by_id = AsyncMock(side_effect=resolve_channel)
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="200",
        chat_type="thread",
        thread_id="200",
        parent_chat_id="100",
        user_id="user-1",
    )

    handle = await adapter.initialize_goal_feature_summary_for_source(
        source,
        initial_request="/goal Follow up on the todos from this meeting.",
        project_context={"project_name": "Hermes Project"},
    )

    assert handle is not None
    assert handle["thread_id"] == "200"
    assert handle["kanban_board"]["slug"] == "discord-200"
    fields = {field.name: field.value for field in thread.sent[0][0]["embed"].fields}
    assert "Kanban Board" in fields


@pytest.mark.asyncio
async def test_tagged_thread_action_followup_creates_request_scoped_feature_summary(adapter, monkeypatch):
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "true")
    parent = FakeTextChannel(channel_id=100, topic="Existing channel note")
    thread = FakeThread(channel_id=200, parent=parent)
    handle = await adapter.initialize_feature_summary(
        thread,
        parent_channel=parent,
        initial_request="Build a deploy dashboard",
    )
    assert handle is not None
    adapter._classify_discord_runtime_mode = AsyncMock(
        return_value=gateway_run.RuntimeMode.ACTION
    )
    adapter.handle_message.reset_mock()

    followup = _make_message(
        adapter,
        channel=thread,
        content="<@999> Also add export",
        message_id=501,
    )
    await adapter._handle_message(followup)

    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.source.chat_type == "thread"
    assert event.feature_summary["thread_id"] == "200"
    assert event.feature_summary["message_id"] == "301"
    assert event.feature_summary["source_message_id"] == "501"
    assert event._discord_promotion_created_feature_summary is True
    assert thread.sent[1][0]["reference"] is followup
    assert event.feature_summary["_thread_obj"] is thread


@pytest.mark.asyncio
async def test_thread_goal_message_creates_per_message_feature_summary(adapter, monkeypatch, tmp_path):
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "true")
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    monkeypatch.setenv("HERMES_PUBLIC_KANBAN_BASE_URL", "https://kanban.example")
    parent = FakeTextChannel(channel_id=100, topic="Existing channel note")
    thread = FakeThread(channel_id=200, parent=parent)
    top_handle = await adapter.initialize_feature_summary(
        thread,
        parent_channel=parent,
        initial_request="Build a deploy dashboard",
    )
    assert top_handle is not None
    adapter.handle_message.reset_mock()

    first = _make_message(adapter, channel=thread, content="<@999> /goal Ship the dashboard", message_id=501)
    await adapter._handle_message(first)

    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert len(thread.sent) == 2
    assert thread.sent[1][0]["reference"] is first
    assert event.text == "/goal Ship the dashboard"
    assert event.feature_summary["thread_id"] == "200"
    assert event.feature_summary["message_id"] == "301"
    assert event.feature_summary["source_message_id"] == "501"
    assert event.feature_summary["kanban_board"]["slug"] == "discord-200-m-501"

    adapter.handle_message.reset_mock()
    second = _make_message(adapter, channel=thread, content="<@999> /goal Ship another goal", message_id=502)
    await adapter._handle_message(second)

    event = adapter.handle_message.await_args.args[0]
    assert len(thread.sent) == 3
    assert thread.sent[2][0]["reference"] is second
    assert event.text == "/goal Ship another goal"
    assert event.feature_summary["message_id"] == "302"
    assert event.feature_summary["source_message_id"] == "502"
    assert event.feature_summary["kanban_board"]["slug"] == "discord-200-m-502"

    state = adapter._read_project_summary_state()
    bucket = state["_feature_summaries"]
    assert bucket["5:200"]["message_id"] == "300"
    assert bucket["5:200:501"]["message_id"] == "301"
    assert bucket["5:200:502"]["message_id"] == "302"
    latest = adapter._load_feature_summary_handle_by_thread_id("200")
    assert latest["message_id"] == "302"
    assert latest["source_message_id"] == "502"


@pytest.mark.asyncio
async def test_parent_goal_with_markdown_attachment_creates_goal_feature_summary(adapter, monkeypatch, tmp_path):
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "true")
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    monkeypatch.setenv("HERMES_PUBLIC_KANBAN_BASE_URL", "https://kanban.example")
    parent = FakeTextChannel(channel_id=100, topic="Existing channel note")
    thread = FakeThread(channel_id=200, parent=parent)
    adapter._auto_create_thread = AsyncMock(return_value=thread)
    attachment = FakeAttachment(
        filename="goalplan.md",
        content_type="text/markdown",
        data=b"# Goalplan\nShip the dashboard safely.",
    )

    await adapter._handle_message(
        _make_message(
            adapter,
            channel=parent,
            content="<@999> /goal",
            message_id=601,
            attachments=[attachment],
        )
    )

    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert len(thread.sent) == 1
    fields = {field.name: field.value for field in thread.sent[0][0]["embed"].fields}
    assert "Kanban Board" in fields
    assert event.text.startswith("/goal\n\n[Content of goalplan.md]:")
    assert "# Goalplan" in event.text
    assert event.message_type == MessageType.COMMAND
    assert event.feature_summary["thread_id"] == "200"
    assert event.feature_summary["source_message_id"] == "601"
    assert event.feature_summary["kanban_board"]["slug"] == "discord-200-m-601"


@pytest.mark.asyncio
async def test_thread_feature_message_does_not_create_request_scoped_kanban_board(adapter, monkeypatch, tmp_path):
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "true")
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    monkeypatch.setenv("HERMES_PUBLIC_KANBAN_BASE_URL", "https://kanban.example")
    parent = FakeTextChannel(channel_id=100, topic="Existing channel note")
    thread = FakeThread(channel_id=200, parent=parent)

    message = _make_message(adapter, channel=thread, content="<@999> Add CSV export", message_id=503)
    await adapter._handle_message(message)

    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.text == "Add CSV export"
    assert event.message_type == MessageType.TEXT
    assert event.feature_summary["thread_id"] == "200"
    assert event.feature_summary["message_id"] == "300"
    assert event.feature_summary["source_message_id"] == "503"
    assert event.feature_summary["kanban_board"] is None
    assert thread.sent[0][0]["reference"] is message


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


def test_load_feature_summary_repairs_missing_project_context(adapter):
    parent = FakeTextChannel(channel_id=100)
    thread = FakeThread(channel_id=200, parent=parent)
    adapter._persist_feature_summary_handle(
        thread,
        {
            "thread_id": "200",
            "message_id": "300",
            "parent_channel_id": "100",
            "initial_request": "/goal speed up q&a",
            "project_context": None,
        },
    )
    adapter._write_project_summary_state.reset_mock()
    project_context = {
        "project_channel_id": "100",
        "project_name": "Hermes",
        "project_path": "/home/droid/hermes",
        "project_github_url": "https://github.com/sligo-droid/hermes-agent",
        "project_mapping_source": "configured_channel_cwd",
        "project_mapping_resolved": True,
    }

    handle = adapter._load_feature_summary_handle_for_thread(
        thread,
        project_context=project_context,
    )

    assert handle["project_context"] == project_context
    adapter._write_project_summary_state.assert_called_once()


def test_load_feature_summary_keeps_existing_project_path(adapter):
    parent = FakeTextChannel(channel_id=100)
    thread = FakeThread(channel_id=200, parent=parent)
    existing_context = {
        "project_channel_id": "999",
        "project_name": "Existing",
        "project_path": "/repo/existing",
        "project_mapping_resolved": True,
    }
    adapter._persist_feature_summary_handle(
        thread,
        {
            "thread_id": "200",
            "message_id": "300",
            "parent_channel_id": "100",
            "initial_request": "/goal speed up q&a",
            "project_context": existing_context,
        },
    )
    adapter._write_project_summary_state.reset_mock()

    handle = adapter._load_feature_summary_handle_for_thread(
        thread,
        project_context={
            "project_channel_id": "100",
            "project_name": "Hermes",
            "project_path": "/home/droid/hermes",
            "project_mapping_resolved": True,
        },
    )

    assert handle["project_context"] == existing_context
    adapter._write_project_summary_state.assert_not_called()


def test_load_feature_summary_ignores_old_source_message(adapter):
    old_id = _discord_snowflake_at(time.time() - (8 * 24 * 60 * 60))
    parent = FakeTextChannel(channel_id=100)
    thread = FakeThread(channel_id=int(old_id), parent=parent)
    adapter._persist_feature_summary_handle(
        thread,
        {
            "thread_id": old_id,
            "message_id": _discord_snowflake_at(time.time()),
            "source_message_id": old_id,
            "parent_channel_id": "100",
            "initial_request": "/goal old work",
        },
    )

    assert adapter._load_feature_summary_handle_for_thread(thread) is None


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
            "priorities": "Refresh from canonical project metadata",
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
async def test_feature_summary_update_edits_initial_message(adapter, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
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
    assert fields["Branch"] == "[feature/summary](https://github.com/acme/hermes-project/tree/feature/summary)"
    assert "Feature Branch URL" not in fields


@pytest.mark.asyncio
async def test_feature_summary_resumed_completion_updates_source_reaction(adapter, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    parent = FakeTextChannel(channel_id=100)
    source_message = SimpleNamespace(
        id=501,
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
        reactions=[],
    )
    origin_message = SimpleNamespace(
        id=200,
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
        reactions=[],
    )

    async def fetch_parent_message(message_id):
        messages = {
            source_message.id: source_message,
            origin_message.id: origin_message,
        }
        return messages[int(message_id)]

    setattr(parent, "fetch_message", AsyncMock(side_effect=fetch_parent_message))
    thread = FakeThread(channel_id=origin_message.id, parent=parent)
    handle = await adapter.initialize_feature_summary(
        thread,
        parent_channel=parent,
        initial_request="Ship project links",
        source_message_id=str(source_message.id),
    )
    assert handle is not None
    assert handle["kanban_board"] is None

    assert await adapter.update_feature_summary(
        handle,
        final_response="The first attempt crashed.",
        status="Failed",
        title="Project Links",
    )
    message = handle["_message_obj"]
    failed_fields = {field.name: field.value for field in message.edit.await_args.kwargs["embed"].fields}
    assert failed_fields["Status"] == "❌ Failed"
    assert message.add_reaction.await_args_list[-1].args == ("❌",)
    assert source_message.add_reaction.await_args_list[-1].args == ("❌",)
    assert origin_message.add_reaction.await_args_list[-1].args == ("❌",)

    message.reactions = [SimpleNamespace(emoji="❌", me=True)]
    source_message.reactions = [SimpleNamespace(emoji="❌", me=True)]
    origin_message.reactions = [SimpleNamespace(emoji="❌", me=True)]
    message.edit.reset_mock()
    message.add_reaction.reset_mock()
    message.remove_reaction.reset_mock()
    source_message.add_reaction.reset_mock()
    source_message.remove_reaction.reset_mock()
    origin_message.add_reaction.reset_mock()
    origin_message.remove_reaction.reset_mock()

    assert await adapter.update_feature_summary(
        handle,
        final_response="Done. The repo and production links are visible.",
        status="Complete",
        title="Project Links",
    )

    complete_fields = {field.name: field.value for field in message.edit.await_args.kwargs["embed"].fields}
    assert complete_fields["Status"] == "✅ Done"
    message.remove_reaction.assert_any_await("❌", adapter._client.user)
    message.add_reaction.assert_awaited_once_with("✅")
    source_message.remove_reaction.assert_any_await("❌", adapter._client.user)
    source_message.add_reaction.assert_awaited_once_with("✅")
    origin_message.remove_reaction.assert_any_await("❌", adapter._client.user)
    origin_message.add_reaction.assert_awaited_once_with("✅")


@pytest.mark.asyncio
async def test_feature_summary_update_skips_unchanged_embed(adapter, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    parent = FakeTextChannel(channel_id=100)
    thread = FakeThread(channel_id=200, parent=parent)
    handle = await adapter.initialize_feature_summary(
        thread,
        parent_channel=parent,
        initial_request="Ship project links",
    )

    assert await adapter.update_feature_summary(
        handle,
        final_response="Done.",
        status="Complete",
        title="Project Links",
    )
    assert await adapter.update_feature_summary(
        handle,
        final_response="Done.",
        status="Complete",
        title="Project Links",
    )

    message = handle["_message_obj"]
    message.edit.assert_awaited_once()


@pytest.mark.asyncio
async def test_feature_summary_update_preserves_existing_artifacts_when_omitted(
    adapter,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    parent = FakeTextChannel(channel_id=100)
    thread = FakeThread(channel_id=200, parent=parent)
    handle = await adapter.initialize_feature_summary(
        thread,
        parent_channel=parent,
        initial_request="Ship trace links",
    )
    artifact = {
        "kind": "external_url",
        "label": "Agent Trace",
        "url": "https://artifacts.example.test/runs/abc",
    }

    assert await adapter.update_feature_summary(
        handle,
        final_response="Done.",
        status="Complete",
        title="Trace links",
        artifacts=[artifact],
    )
    message = handle["_message_obj"]
    message.edit.reset_mock()

    assert await adapter.update_feature_summary(
        handle,
        final_response="Board sync updated the outcome.",
        status="Blocked",
        title="Trace links",
    )

    fields = {
        field.name: field.value
        for field in message.edit.await_args.kwargs["embed"].fields
    }
    assert fields["Agent Trace"] == (
        "[Open link](https://artifacts.example.test/runs/abc)"
    )


@pytest.mark.asyncio
async def test_feature_summary_update_backs_off_after_rate_limit(adapter, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    parent = FakeTextChannel(channel_id=100)
    thread = FakeThread(channel_id=200, parent=parent)
    handle = await adapter.initialize_feature_summary(
        thread,
        parent_channel=parent,
        initial_request="Ship project links",
    )
    message = handle["_message_obj"]

    class RateLimitError(Exception):
        retry_after = 10

    message.edit.side_effect = RateLimitError("rate limited")
    monkeypatch.setattr(adapter, "_is_discord_rate_limit", lambda exc: isinstance(exc, RateLimitError))

    assert await adapter.update_feature_summary(
        handle,
        final_response="Done.",
        status="Complete",
        title="Project Links",
    ) is False
    assert await adapter.update_feature_summary(
        handle,
        final_response="Done again.",
        status="Complete",
        title="Project Links",
    ) is False

    message.edit.assert_awaited_once()


@pytest.mark.asyncio
async def test_worker_task_thread_does_not_seed_feature_summary(adapter):
    parent = FakeTextChannel(channel_id=100)
    thread = FakeThread(channel_id=201, parent=parent)
    parent.create_thread = AsyncMock(return_value=thread)
    adapter._client.get_channel = lambda channel_id: parent if int(channel_id) == 100 else None

    handle = await adapter.create_worker_task_thread(
        "100",
        name="dev: Build dashboard filters",
        title="Build dashboard filters",
        initial_request="Build dashboard filters\n\nAdd filter controls.",
        project_context={"project_name": "Hermes"},
        kanban_url="https://hermes.example.test/workers/123/tickets/t1",
        source_board="discord-123",
        source_task_id="t1",
        source_task_url="https://hermes.example.test/workers/123/tickets/t1",
        source_kanban_url="https://hermes.example.test/workers/123",
        source_discord_thread_url="https://discord.com/channels/5/123",
    )

    assert handle == {
        "thread_id": "201",
        "thread_name": "feature-thread",
        "message_id": "",
    }
    parent.create_thread.assert_awaited_once()
    assert thread.sent == []


@pytest.mark.asyncio
async def test_worker_task_embed_does_not_post_to_existing_thread(adapter):
    parent = FakeTextChannel(channel_id=100)
    thread = FakeThread(channel_id=202, parent=parent)

    adapter._client.get_channel = lambda channel_id: thread if int(channel_id) == 202 else None

    handle = await adapter.send_worker_task_embed(
        "202",
        title="Build dashboard filters",
        initial_request="Build dashboard filters\n\nAdd filter controls.",
        project_context={"project_name": "Hermes"},
        kanban_url="https://hermes.example.test/workers/123/tickets/t1",
        source_board="discord-123",
        source_task_id="t1",
        source_task_url="https://hermes.example.test/workers/123/tickets/t1",
        source_kanban_url="https://hermes.example.test/workers/123",
        source_discord_thread_url="https://discord.com/channels/5/123",
    )

    assert handle == {
        "thread_id": "202",
        "thread_name": "feature-thread",
        "message_id": "",
    }
    assert thread.sent == []


@pytest.mark.asyncio
async def test_foreman_goal_embed_posts_to_source_thread_and_hides_source_links(adapter):
    parent = FakeTextChannel(channel_id=100)
    thread = FakeThread(channel_id=202, parent=parent)
    adapter._client.get_channel = lambda channel_id: thread if int(channel_id) == 202 else None

    handle = await adapter.create_foreman_goal_thread(
        "202",
        name="Foreman: fix discord-123/t1",
        initial_request="/goal Foreman escalation: resolve t1",
        source_board="discord-123",
        source_task_id="t1",
        source_task_url="https://hermes.example.test/workers/123/tickets/t1",
        source_kanban_url="https://hermes.example.test/workers/123",
        source_discord_thread_url="https://discord.com/channels/5/123",
        kanban_board={"slug": "foreman-abc", "public_url": "https://hermes.example.test/workers/foreman-abc"},
    )

    assert handle is not None
    assert handle["title"] == "Foreman: fix discord-123/t1"
    assert handle["source_board"] == "discord-123"
    assert handle["source_task_url"] == "https://hermes.example.test/workers/123/tickets/t1"
    assert handle["hide_source_links"] is True
    assert handle["thread_id"] == "202"
    assert handle["summary_channel_id"] == "202"
    sent_embed = thread.sent[0][0]["embed"]
    sent_message = thread.sent[0][1]
    fields = {field.name: field.value for field in sent_embed.fields}
    assert sent_embed.title == "Foreman: fix discord-123/t1"
    assert "Affected Board" not in fields
    assert "Affected Task" not in fields
    assert "Discord Thread" not in fields
    assert fields["Foreman Kanban"] == "https://hermes.example.test/workers/foreman-abc"
    sent_message.add_reaction.assert_awaited_once_with("⏳")


@pytest.mark.asyncio
async def test_foreman_feature_summary_uses_source_task_state_over_done_board(adapter, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    parent = FakeTextChannel(channel_id=100)
    thread = FakeThread(channel_id=202, parent=parent)
    adapter._client.get_channel = lambda channel_id: thread if int(channel_id) == 202 else None

    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db

    conn = kanban_db.connect(board=kanban_db.DEFAULT_BOARD)
    try:
        source_task = kanban_db.create_task(conn, title="Default intake", assignee="default")
    finally:
        conn.close()

    foreman_board = dwb.start_direct_goal(
        thread_id="202",
        goal=(
            "/goal Foreman escalation: resolve a Discord worker issue.\n\n"
            "Problem:\n"
            "- Board: default\n"
            f"- Task: {source_task}\n"
        ),
        board_slug="foreman-source-summary",
    )
    dwb._update_worker_meta(foreman_board.slug, {"goal_status": "done", "phase": "complete"})

    handle = await adapter.create_foreman_goal_thread(
        "202",
        name="Foreman: fix default intake",
        initial_request="/goal Foreman escalation: resolve default intake",
        source_board=kanban_db.DEFAULT_BOARD,
        source_task_id=source_task,
        kanban_board={"slug": foreman_board.slug, "public_url": "https://hermes.example/workers/foreman-source-summary"},
    )
    assert handle is not None
    message = thread.sent[0][1]
    message.add_reaction.reset_mock()

    synced = await adapter.sync_kanban_feature_summary(
        {
            "board": foreman_board.slug,
            "thread_id": "202",
            "message_id": handle["message_id"],
            "source_message_id": handle["source_message_id"],
            "source_board": kanban_db.DEFAULT_BOARD,
            "source_task_id": source_task,
            "state": "done",
            "sync_key": "source-active",
        }
    )

    assert synced == "source-active"
    active_fields = {field.name: field.value for field in message.edit.await_args.kwargs["embed"].fields}
    assert active_fields["Status"] == "⏳ In progress"
    message.add_reaction.assert_not_awaited()

    conn = kanban_db.connect(board=kanban_db.DEFAULT_BOARD)
    try:
        claimed = kanban_db.claim_task(conn, source_task)
        assert claimed is not None
        kanban_db.complete_task(conn, source_task, summary="done", expected_run_id=claimed.current_run_id)
    finally:
        conn.close()

    message.edit.reset_mock()
    message.add_reaction.reset_mock()
    done_synced = await adapter.sync_kanban_feature_summary(
        {
            "board": foreman_board.slug,
            "thread_id": "202",
            "message_id": handle["message_id"],
            "source_message_id": handle["source_message_id"],
            "source_board": kanban_db.DEFAULT_BOARD,
            "source_task_id": source_task,
            "state": "done",
            "sync_key": "source-done",
        }
    )

    assert done_synced == "source-done"
    done_fields = {field.name: field.value for field in message.edit.await_args.kwargs["embed"].fields}
    assert done_fields["Status"] == "✅ Done"
    message.add_reaction.assert_awaited_once_with("✅")


def test_feature_summary_kanban_status_labels(adapter):
    adapter._feature_kanban_reaction_state = MagicMock(return_value="active")
    active_embed = adapter._build_feature_summary_embed(
        initial_request="",
        status=adapter._feature_kanban_summary_status({"kanban_board": {"slug": "board"}}) or "Running",
    )
    active_fields = {field.name: field.value for field in active_embed.fields}
    assert active_fields["Status"] == "⏳ In progress"

    adapter._feature_kanban_reaction_state = MagicMock(return_value="running")
    running_embed = adapter._build_feature_summary_embed(
        initial_request="",
        status=adapter._feature_kanban_summary_status({"kanban_board": {"slug": "board"}}) or "Running",
    )
    running_fields = {field.name: field.value for field in running_embed.fields}
    assert running_fields["Status"] == "⏳ Running"

    adapter._feature_kanban_reaction_state = MagicMock(return_value="done")
    done_embed = adapter._build_feature_summary_embed(
        initial_request="",
        status=adapter._feature_kanban_summary_status({"kanban_board": {"slug": "board"}}) or "Running",
    )
    done_fields = {field.name: field.value for field in done_embed.fields}
    assert done_fields["Status"] == "✅ Done"

    adapter._feature_kanban_reaction_state = MagicMock(return_value="blocked")
    blocked_embed = adapter._build_feature_summary_embed(
        initial_request="",
        status=adapter._feature_kanban_summary_status({"kanban_board": {"slug": "board"}}) or "Running",
    )
    blocked_fields = {field.name: field.value for field in blocked_embed.fields}
    assert blocked_fields["Status"] == "❓ Blocked"

    foreman_embed = adapter._build_feature_summary_embed(
        initial_request="",
        status="Foreman",
    )
    foreman_fields = {field.name: field.value for field in foreman_embed.fields}
    assert foreman_fields["Status"] == "🔨 Foreman"


@pytest.mark.asyncio
async def test_goal_feature_summary_uses_absolute_kanban_board_url(adapter, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    monkeypatch.setenv("HERMES_PUBLIC_KANBAN_BASE_URL", "https://hermes.sligolabs.com")
    parent = FakeTextChannel(channel_id=100)
    thread = FakeThread(channel_id=200, parent=parent)

    handle = await adapter.initialize_feature_summary(
        thread,
        parent_channel=parent,
        initial_request="/goal Ship project links",
    )

    assert handle is not None
    sent_embed = thread.sent[0][0]["embed"]
    fields = {field.name: field.value for field in sent_embed.fields}
    assert fields["Kanban Board"] == "https://hermes.sligolabs.com/workers/200"


@pytest.mark.asyncio
async def test_kanban_feature_summary_update_uses_board_snapshot(adapter, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    monkeypatch.setenv("HERMES_PUBLIC_KANBAN_BASE_URL", "https://hermes.sligolabs.com")
    parent = FakeTextChannel(channel_id=100)
    thread = FakeThread(channel_id=200, parent=parent)

    handle = await adapter.initialize_feature_summary(
        thread,
        parent_channel=parent,
        initial_request="/goal Ship project links",
    )
    assert handle is not None

    from hermes_cli import discord_worker_boards as dwb

    board = dwb.set_goal(thread_id="200", goal="Ship project links")
    dwb.set_feature_summary_title(board.slug, "Project Link Summary")

    assert await adapter.update_feature_summary(
        handle,
        final_response="This direct response should not replace Kanban state.",
        status="Complete",
    )

    message = handle["_message_obj"]
    edited_embed = message.edit.await_args.kwargs["embed"]
    fields = {field.name: field.value for field in edited_embed.fields}
    assert edited_embed.title == "Project Link Summary"
    assert fields["Status"] == "⏳ In progress"
    assert fields["Concise Outcome"] != "Pending"
    assert "Kanban" in fields["Concise Outcome"] or "ticket" in fields["Concise Outcome"]
    assert fields["Branch"] == "[discord/200](https://github.com/acme/hermes-project/tree/discord/200)"
    assert fields["Kanban Board"] == "https://hermes.sligolabs.com/workers/200"
    assert "Feature Branch URL" not in fields


@pytest.mark.asyncio
async def test_sync_kanban_feature_summary_reopens_persisted_handle(adapter, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    monkeypatch.setenv("HERMES_PUBLIC_KANBAN_BASE_URL", "https://hermes.sligolabs.com")
    parent = FakeTextChannel(channel_id=100)
    thread = FakeThread(channel_id=200, parent=parent)
    adapter._client.get_channel = lambda channel_id: thread if int(channel_id) == 200 else None

    handle = await adapter.initialize_feature_summary(
        thread,
        parent_channel=parent,
        initial_request="/goal Ship the dashboard",
    )
    assert handle is not None

    from hermes_cli import discord_worker_boards as dwb

    board = dwb.set_goal(thread_id="200", goal="Ship the dashboard")
    synced = await adapter.sync_kanban_feature_summary(
        {
            "board": board.slug,
            "thread_id": "200",
            "state": "active",
            "title": "Dashboard Shipment",
            "public_url": "https://hermes.sligolabs.com/workers/200",
            "sync_key": "sync-1",
        }
    )

    assert synced == "sync-1"
    message = handle["_message_obj"]
    edited_embed = message.edit.await_args.kwargs["embed"]
    fields = {field.name: field.value for field in edited_embed.fields}
    assert edited_embed.title == "Dashboard Shipment"
    assert fields["Branch"] == "[discord/200](https://github.com/acme/hermes-project/tree/discord/200)"


@pytest.mark.asyncio
async def test_sync_kanban_feature_summary_persists_late_board_attachment(adapter, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    monkeypatch.setenv("HERMES_PUBLIC_KANBAN_BASE_URL", "https://hermes.sligolabs.com")
    parent = FakeTextChannel(channel_id=100)
    thread = FakeThread(channel_id=200, parent=parent)
    adapter._client.get_channel = lambda channel_id: thread if int(channel_id) == 200 else None

    handle = await adapter.initialize_feature_summary(
        thread,
        parent_channel=parent,
        initial_request="Implement the feature from this thread",
    )
    assert handle is not None
    assert handle["kanban_board"] is None

    from hermes_cli import discord_worker_boards as dwb

    board = dwb.set_goal(thread_id="200", goal="Ship the dashboard")
    synced = await adapter.sync_kanban_feature_summary(
        {
            "board": board.slug,
            "thread_id": "200",
            "state": "active",
            "public_url": "https://hermes.sligolabs.com/workers/200",
            "sync_key": "sync-late-attach",
        }
    )

    assert synced == "sync-late-attach"
    state = adapter._read_project_summary_state()
    stored = state["_feature_summaries"]["5:200"]
    assert stored["kanban_board"] == {
        "slug": "discord-200",
        "public_url": "https://hermes.sligolabs.com/workers/200",
    }
    message = handle["_message_obj"]
    edited_embed = message.edit.await_args.kwargs["embed"]
    fields = {field.name: field.value for field in edited_embed.fields}
    assert fields["Kanban Board"] == "https://hermes.sligolabs.com/workers/200"


@pytest.mark.asyncio
async def test_sync_kanban_feature_summary_uses_persisted_terminal_summary(adapter, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    monkeypatch.setenv("HERMES_PUBLIC_KANBAN_BASE_URL", "https://hermes.sligolabs.com")
    parent = FakeTextChannel(channel_id=100)
    thread = FakeThread(channel_id=200, parent=parent)
    adapter._client.get_channel = lambda channel_id: thread if int(channel_id) == 200 else None

    handle = await adapter.initialize_feature_summary(
        thread,
        parent_channel=parent,
        initial_request="/goal Ship the dashboard",
    )
    assert handle is not None

    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db

    board = dwb.start_direct_goal(thread_id="200", goal="Ship the dashboard")
    conn = kanban_db.connect(board=board.slug)
    try:
        task_id = kanban_db.create_task(conn, title="Build dashboard", assignee=dwb.ROLE_DEV, tenant=board.slug)
        claimed = kanban_db.claim_task(conn, task_id)
        assert claimed is not None
        kanban_db.complete_task(conn, task_id, summary="Dashboard built.", expected_run_id=claimed.current_run_id)
    finally:
        conn.close()
    dwb._update_worker_meta(
        board.slug,
        {
            "goal_status": "done",
            "phase": "complete",
            "pr_url": "https://github.com/acme/hermes-project/pull/44",
            "pr_state": "OPEN",
            "pr_checks_status": "passed",
            "preview_url": "https://hermes-project-thread-44.vercel.app",
            "preview_status": "ready",
            "terminal_summary_sync_pending": True,
        },
    )
    dwb.persist_board_run_summary(board.slug)

    synced = await adapter.sync_kanban_feature_summary(
        {
            "board": board.slug,
            "thread_id": "200",
            "state": "done",
            "sync_key": "terminal-sync",
        }
    )

    assert synced == "terminal-sync"
    message = handle["_message_obj"]
    edited_embed = message.edit.await_args.kwargs["embed"]
    fields = {field.name: field.value for field in edited_embed.fields}
    assert fields["Status"] == "✅ Done"
    assert "Checks: passed" in fields["Concise Outcome"]
    assert "Preview: https://hermes-project-thread-44.vercel.app (ready)" in fields["Concise Outcome"]
    assert message.add_reaction.await_args_list[-1].args == ("✅",)
    worker = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert "terminal_summary_sync_pending" not in worker


@pytest.mark.asyncio
async def test_sync_kanban_feature_summary_updates_status_after_terminal_recovery(adapter, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    parent = FakeTextChannel(channel_id=101)
    thread = FakeThread(channel_id=201, parent=parent)
    adapter._client.get_channel = lambda channel_id: thread if int(channel_id) == 201 else None

    handle = await adapter.initialize_feature_summary(
        thread,
        parent_channel=parent,
        initial_request="/goal Recover after failure",
    )
    assert handle is not None

    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db

    board = dwb.start_direct_goal(thread_id="201", goal="Recover after failure")
    dwb._update_worker_meta(
        board.slug,
        {
            "goal_status": "blocked",
            "phase": "blocked",
            "blocked_reason": "worker crashed",
        },
    )
    blocked_sync = await adapter.sync_kanban_feature_summary(
        {
            "board": board.slug,
            "thread_id": "201",
            "state": "blocked",
            "sync_key": "blocked-sync",
        }
    )
    assert blocked_sync == "blocked-sync"
    message = handle["_message_obj"]
    blocked_fields = {field.name: field.value for field in message.edit.await_args.kwargs["embed"].fields}
    assert blocked_fields["Status"] == "❓ Blocked"

    dwb._update_worker_meta(
        board.slug,
        {
            "goal_status": "done",
            "phase": "complete",
            "blocked_reason": "",
        },
    )
    target = next(item for item in dwb.thread_status_targets() if item["board"] == board.slug)
    assert target["state"] == "done"
    assert target["terminal_summary_sync_pending"] is True

    done_sync = await adapter.sync_kanban_feature_summary(
        {
            "board": board.slug,
            "thread_id": "201",
            "state": "done",
            "sync_key": "done-sync",
        }
    )

    assert done_sync == "done-sync"
    done_fields = {field.name: field.value for field in message.edit.await_args.kwargs["embed"].fields}
    assert done_fields["Status"] == "✅ Done"
    worker = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert "terminal_summary_sync_pending" not in worker


@pytest.mark.asyncio
async def test_send_kanban_completion_notice_posts_once(adapter, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    parent = FakeTextChannel(channel_id=100)
    thread = FakeThread(channel_id=200, parent=parent)
    adapter._client.get_channel = lambda channel_id: thread if int(channel_id) == 200 else None
    adapter._client.fetch_channel = AsyncMock(return_value=thread)

    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db

    board = dwb.start_direct_goal(thread_id="200", goal="Ship the dashboard")
    dwb._update_worker_meta(
        board.slug,
        {
            "goal_status": "done",
            "phase": "complete",
            "terminal_completion_message_pending": True,
        },
    )

    target = {
        "board": board.slug,
        "thread_id": "200",
        "chat_id": "200",
        "state": "done",
        "outcome": "Done. Tasks: done:1 total:1. PR: https://github.example/pr/1.",
        "pr_url": "https://github.example/pr/1",
        "public_url": "https://hermes.example/workers/200",
        "terminal_completion_message_pending": True,
    }
    sent = await adapter.send_kanban_completion_notice(target)

    assert sent == board.slug
    assert len(thread.sent) == 1
    kwargs, _message = thread.sent[0]
    assert kwargs["content"].startswith("Completed.\n\nWhat changed:")
    assert "https://github.example/pr/1" in kwargs["content"]
    assert "Shipped:" in kwargs["content"]
    assert "- Worker: https://hermes.example/workers/200" in kwargs["content"]
    assert "Board summary:" not in kwargs["content"]
    worker = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert "terminal_completion_message_pending" not in worker


@pytest.mark.asyncio
async def test_send_kanban_completion_notice_renders_human_fallback_summary(adapter, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    parent = FakeTextChannel(channel_id=100)
    thread = FakeThread(channel_id=200, parent=parent)
    adapter._client.get_channel = lambda channel_id: thread if int(channel_id) == 200 else None
    adapter._client.fetch_channel = AsyncMock(return_value=thread)

    from hermes_cli import discord_worker_boards as dwb

    board = dwb.start_direct_goal(thread_id="200", goal="Ship the dashboard")
    dwb._update_worker_meta(
        board.slug,
        {
            "goal_status": "done",
            "phase": "complete",
            "terminal_completion_message_pending": True,
        },
    )

    sent = await adapter.send_kanban_completion_notice(
        {
            "board": board.slug,
            "thread_id": "200",
            "chat_id": "200",
            "state": "done",
            "terminal_completion_message_pending": True,
            "board_summary": {
                "title": "Ship the dashboard",
                "outcome": "Done. Tasks: done:3. PR: https://github.example/pr/1.",
                "latest_tasks": [
                    {
                        "assignee": "reviewer",
                        "status": "done",
                        "latest_summary": "Approved. The implementation is covered and ready to ship.",
                    },
                    {
                        "assignee": "developer",
                        "status": "done",
                        "latest_summary": "Implemented dataset-scoped Census helper and Data Ledger wiring.",
                    },
                    {
                        "assignee": "planner",
                        "status": "done",
                        "latest_summary": "Confirmed the requested Federal Census panel scope.",
                    },
                ],
                "review": {
                    "final_verdict": {
                        "status": "approved",
                        "summary": "Approved. The implementation is covered and ready to ship.",
                    }
                },
                "verification_commands": [
                    {"command": "pnpm --dir dashboard check", "result": "passed"},
                    {"command": "git diff --check", "result": "passed"},
                ],
                "pr": {
                    "url": "https://github.example/pr/1",
                    "merge_state": "merged",
                    "merge_commit": "abcdef1234567890",
                    "checks_status": "passed",
                    "checks_total": 4,
                },
                "deployment_status": "done",
                "branch": "discord/200",
                "task_counts": {"done": 3, "total": 3},
                "public_url": "https://hermes.example/workers/200",
            },
        }
    )

    assert sent == board.slug
    assert len(thread.sent) == 1
    content = thread.sent[0][0]["content"]
    assert content.startswith("Completed.\n\nWhat changed:")
    what_changed = content.split("\n\nVerification:", 1)[0]
    assert "- Implemented dataset-scoped Census helper and Data Ledger wiring." in content
    assert "- Confirmed the requested Federal Census panel scope." in content
    assert "- PR: https://github.example/pr/1" not in what_changed
    assert "Verification:" in content
    assert "- `pnpm --dir dashboard check` → `passed`" in content
    assert "- PR checks: `passed` (4 checks)" in content
    assert "- Deployment: `done`" in content
    assert "Shipped:" in content
    assert "- PR: https://github.example/pr/1" in content
    assert "- Merged: `abcdef123456`" in content
    assert "- Branch: `discord/200`" in content
    assert "- Worker tasks: `3 done`" in content
    assert "- Worker: https://hermes.example/workers/200" in content
    assert "Board summary:" not in content
    assert "Kanban goal:" not in content
    assert "Runs:" not in content
    assert "Runtime:" not in content


@pytest.mark.asyncio
async def test_send_kanban_completion_notice_uses_full_final_response(adapter, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    parent = FakeTextChannel(channel_id=100)
    thread = FakeThread(channel_id=200, parent=parent)
    adapter._client.get_channel = lambda channel_id: thread if int(channel_id) == 200 else None
    adapter._client.fetch_channel = AsyncMock(return_value=thread)

    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db

    board = dwb.start_direct_goal(thread_id="200", goal="Ship the dashboard")
    dwb._update_worker_meta(
        board.slug,
        {
            "goal_status": "done",
            "phase": "complete",
            "terminal_completion_message_pending": True,
        },
    )
    final_response = "Completed.\n\nWhat changed:\nRoot cause confirmed and fixed.\n\nVerification:\nFocused tests passed."

    sent = await adapter.send_kanban_completion_notice(
        {
            "board": board.slug,
            "thread_id": "200",
            "chat_id": "200",
            "state": "done",
            "outcome": "Done. Tasks: done:1 total:1.",
            "terminal_completion_message_pending": True,
            "board_summary": {
                "final_response": {"text": final_response},
                "pr": {"url": "https://github.example/pr/1", "merge_state": "merged"},
                "public_url": "https://hermes.example/workers/200",
            },
        }
    )

    assert sent == board.slug
    assert len(thread.sent) == 1
    content = thread.sent[0][0]["content"]
    assert content.startswith("Completed.\n\nWhat changed:")
    assert "Root cause confirmed and fixed." in content
    assert "Verification:" in content
    assert "Merged: https://github.example/pr/1" in content
    assert "Worker: https://hermes.example/workers/200" in content
    assert "✅ Done" not in content
    worker = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert worker["terminal_completion_message_id"] == "300"
    assert "terminal_completion_message_pending" not in worker


@pytest.mark.asyncio
async def test_send_kanban_completion_notice_ignores_stale_success_blocker_final_response(
    adapter,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    parent = FakeTextChannel(channel_id=100)
    thread = FakeThread(channel_id=200, parent=parent)
    adapter._client.get_channel = lambda channel_id: thread if int(channel_id) == 200 else None
    adapter._client.fetch_channel = AsyncMock(return_value=thread)

    from hermes_cli import discord_worker_boards as dwb

    board = dwb.start_direct_goal(thread_id="200", goal="Ship the dashboard")
    dwb._update_worker_meta(
        board.slug,
        {
            "goal_status": "done",
            "phase": "complete",
            "terminal_completion_message_pending": True,
        },
    )

    sent = await adapter.send_kanban_completion_notice(
        {
            "board": board.slug,
            "thread_id": "200",
            "chat_id": "200",
            "state": "done",
            "terminal_completion_message_pending": True,
            "board_summary": {
                "title": "Ship the dashboard",
                "outcome": "Done. PR merged and deployed.",
                "goal_status": "done",
                "phase": "complete",
                "latest_tasks": [
                    {
                        "assignee": "developer",
                        "status": "done",
                        "latest_summary": "Recovered PR finalization and completed the deployment sync.",
                    }
                ],
                "verification_commands": [
                    {"command": "scripts/run_tests.sh tests/gateway/test_discord_summaries.py", "result": "passed"}
                ],
                "pr": {
                    "url": "https://github.example/pr/426",
                    "merge_state": "merged",
                    "merge_commit": "abcdef1234567890",
                    "checks_status": "passed",
                    "checks_total": 7,
                },
                "deployment_status": "done",
                "final_response": {
                    "text": (
                        "Blocker: PR finalization/merge failed because GitHub reports "
                        "the PR as DIRTY / CONFLICTING.\n\nUnblock path: resolve conflicts and rerun finalization."
                    )
                },
            },
        }
    )

    assert sent == board.slug
    assert len(thread.sent) == 1
    content = thread.sent[0][0]["content"]
    assert content.startswith("Completed.\n\nWhat changed:")
    assert "Blocker" not in content
    assert "DIRTY" not in content
    assert "CONFLICTING" not in content
    assert "Unblock path" not in content
    assert "Shipped:" in content
    assert "- PR: https://github.example/pr/426" in content
    assert "- Merged: `abcdef123456`" in content
    assert "- PR checks: `passed` (7 checks)" in content
    assert "- Deployment: `done`" in content


@pytest.mark.asyncio
async def test_send_kanban_completion_notice_posts_all_chunks(adapter, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    parent = FakeTextChannel(channel_id=100)
    thread = FakeThread(channel_id=200, parent=parent)
    adapter._client.get_channel = lambda channel_id: thread if int(channel_id) == 200 else None
    adapter._client.fetch_channel = AsyncMock(return_value=thread)

    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db

    board = dwb.start_direct_goal(thread_id="200", goal="Ship the dashboard")
    dwb._update_worker_meta(
        board.slug,
        {
            "goal_status": "done",
            "phase": "complete",
            "terminal_completion_message_pending": True,
        },
    )
    final_response = "Completed.\n\nWhat changed:\n" + "\n".join(
        f"- verified detailed completion item {idx} with enough text to force chunking" for idx in range(80)
    )

    sent = await adapter.send_kanban_completion_notice(
        {
            "board": board.slug,
            "thread_id": "200",
            "chat_id": "200",
            "state": "done",
            "terminal_completion_message_pending": True,
            "board_summary": {"final_response": {"text": final_response}},
        }
    )

    assert sent == board.slug
    assert len(thread.sent) > 1
    assert all(len(kwargs["content"]) <= adapter.MAX_MESSAGE_LENGTH for kwargs, _message in thread.sent)
    assert "verified detailed completion item 0" in thread.sent[0][0]["content"]
    assert "verified detailed completion item 79" in thread.sent[-1][0]["content"]
    worker = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert worker["terminal_completion_message_id"] == "300"
    assert "terminal_completion_message_pending" not in worker


@pytest.mark.asyncio
async def test_send_kanban_completion_notice_records_first_chunk_before_continuation_failure(adapter, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    parent = FakeTextChannel(channel_id=100)

    class FailAfterFirstThread(FakeThread):
        async def send(self, **kwargs):
            if self.sent:
                raise RuntimeError("continuation send failed")
            return await super().send(**kwargs)

    thread = FailAfterFirstThread(channel_id=200, parent=parent)
    adapter._client.get_channel = lambda channel_id: thread if int(channel_id) == 200 else None
    adapter._client.fetch_channel = AsyncMock(return_value=thread)

    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db

    board = dwb.start_direct_goal(thread_id="200", goal="Ship the dashboard")
    dwb._update_worker_meta(
        board.slug,
        {
            "goal_status": "done",
            "phase": "complete",
            "terminal_completion_message_pending": True,
        },
    )
    final_response = "Completed.\n\nWhat changed:\n" + "\n".join(
        f"- verified detailed completion item {idx} with enough text to force chunking" for idx in range(80)
    )

    sent = await adapter.send_kanban_completion_notice(
        {
            "board": board.slug,
            "thread_id": "200",
            "chat_id": "200",
            "state": "done",
            "terminal_completion_message_pending": True,
            "board_summary": {"final_response": {"text": final_response}},
        }
    )

    assert sent == board.slug
    assert len(thread.sent) == 1
    assert "verified detailed completion item 0" in thread.sent[0][0]["content"]
    worker = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert worker["terminal_completion_message_id"] == "300"
    assert "terminal_completion_message_pending" not in worker


@pytest.mark.asyncio
async def test_send_kanban_completion_notice_suppresses_foreman_success(adapter, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    parent = FakeTextChannel(channel_id=100)
    thread = FakeThread(channel_id=200, parent=parent)
    adapter._client.get_channel = lambda channel_id: thread if int(channel_id) == 200 else None
    adapter._client.fetch_channel = AsyncMock(return_value=thread)

    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db

    board = dwb.start_direct_goal(
        thread_id="200",
        goal="Foreman escalation: resolve a Discord worker issue.",
        board_slug="foreman-success-notice",
    )
    dwb._update_worker_meta(
        board.slug,
        {
            "goal_status": "done",
            "phase": "complete",
            "terminal_completion_message_pending": True,
        },
    )

    target = {
        "board": board.slug,
        "thread_id": "200",
        "chat_id": "200",
        "state": "done",
        "outcome": "Done. Foreman recovered the source board.",
        "terminal_completion_message_pending": True,
        "foreman_generated": True,
    }
    sent = await adapter.send_kanban_completion_notice(target)

    assert sent == board.slug
    assert thread.sent == []
    worker = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert "terminal_completion_message_pending" not in worker


@pytest.mark.asyncio
async def test_sync_kanban_feature_summary_refuses_mismatched_board(adapter, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    parent = FakeTextChannel(channel_id=100)
    thread = FakeThread(channel_id=200, parent=parent)

    handle = await adapter.initialize_feature_summary(
        thread,
        parent_channel=parent,
        initial_request="/goal Ship the dashboard",
    )
    assert handle is not None
    message = handle["_message_obj"]

    synced = await adapter.sync_kanban_feature_summary(
        {
            "board": "discord-201",
            "thread_id": "200",
            "guild_id": "5",
            "parent_channel_id": "100",
            "state": "active",
            "title": "Wrong Thread Title",
            "sync_key": "wrong-thread",
        }
    )

    assert synced is None
    message.edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_kanban_feature_summary_refuses_mismatched_discord_scope(adapter, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    parent = FakeTextChannel(channel_id=100)
    thread = FakeThread(channel_id=200, parent=parent)

    handle = await adapter.initialize_feature_summary(
        thread,
        parent_channel=parent,
        initial_request="/goal Ship the dashboard",
    )
    assert handle is not None
    message = handle["_message_obj"]

    assert await adapter.sync_kanban_feature_summary(
        {
            "board": "discord-200",
            "thread_id": "200",
            "guild_id": "6",
            "parent_channel_id": "100",
            "state": "active",
            "title": "Wrong Guild Title",
            "sync_key": "wrong-guild",
        }
    ) is None
    assert await adapter.sync_kanban_feature_summary(
        {
            "board": "discord-200",
            "thread_id": "200",
            "guild_id": "5",
            "parent_channel_id": "101",
            "state": "active",
            "title": "Wrong Parent Title",
            "sync_key": "wrong-parent",
        }
    ) is None

    message.edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_kanban_feature_summary_circuits_permanent_fetch_failure(adapter, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    parent = FakeTextChannel(channel_id=100)
    thread = FakeThread(channel_id=200, parent=parent)
    adapter._client.get_channel = lambda channel_id: thread if int(channel_id) == 200 else None

    handle = await adapter.initialize_feature_summary(
        thread,
        parent_channel=parent,
        initial_request="/goal Ship the dashboard",
    )
    assert handle is not None
    handle.pop("_message_obj", None)
    thread.fetch_message = AsyncMock(side_effect=discord_platform.discord.NotFound("unknown message"))

    target = {
        "board": "discord-200",
        "thread_id": "200",
        "state": "active",
        "sync_key": "sync-1",
    }
    assert await adapter.sync_kanban_feature_summary(target) == "sync-1"
    assert thread.fetch_message.await_count == 1

    assert await adapter.sync_kanban_feature_summary(target) == "sync-1"
    assert thread.fetch_message.await_count == 1


@pytest.mark.asyncio
async def test_sync_kanban_feature_summary_clears_terminal_flag_after_permanent_fetch_failure(
    adapter,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    parent = FakeTextChannel(channel_id=100)
    thread = FakeThread(channel_id=200, parent=parent)
    adapter._client.get_channel = lambda channel_id: thread if int(channel_id) == 200 else None

    handle = await adapter.initialize_feature_summary(
        thread,
        parent_channel=parent,
        initial_request="/goal Ship the dashboard",
    )
    assert handle is not None
    handle.pop("_message_obj", None)
    thread.fetch_message = AsyncMock(side_effect=discord_platform.discord.NotFound("unknown message"))

    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db

    board = dwb.start_direct_goal(thread_id="200", goal="Ship the dashboard")
    dwb._update_worker_meta(
        board.slug,
        {
            "goal_status": "done",
            "phase": "complete",
            "terminal_summary_sync_pending": True,
        },
    )

    assert await adapter.sync_kanban_feature_summary(
        {
            "board": board.slug,
            "thread_id": "200",
            "state": "done",
            "sync_key": "sync-terminal",
            "terminal_summary_sync_pending": True,
        }
    ) == "sync-terminal"

    worker = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert "terminal_summary_sync_pending" not in worker


@pytest.mark.asyncio
async def test_sync_kanban_feature_summary_clears_terminal_flag_without_handle(
    adapter,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db

    board = dwb.start_direct_goal(thread_id="200", goal="Ship the dashboard")
    dwb._update_worker_meta(
        board.slug,
        {
            "goal_status": "done",
            "phase": "complete",
            "terminal_summary_sync_pending": True,
        },
    )

    assert await adapter.sync_kanban_feature_summary(
        {
            "board": board.slug,
            "thread_id": "200",
            "state": "done",
            "sync_key": "sync-missing-handle",
            "terminal_summary_sync_pending": True,
        }
    ) == "sync-missing-handle"

    worker = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert "terminal_summary_sync_pending" not in worker


@pytest.mark.asyncio
async def test_sync_kanban_feature_summary_transient_failure_remains_retriable(adapter, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    parent = FakeTextChannel(channel_id=100)
    thread = FakeThread(channel_id=200, parent=parent)
    adapter._client.get_channel = lambda channel_id: thread if int(channel_id) == 200 else None

    handle = await adapter.initialize_feature_summary(
        thread,
        parent_channel=parent,
        initial_request="/goal Ship the dashboard",
    )
    assert handle is not None
    handle.pop("_message_obj", None)
    thread.fetch_message = AsyncMock(side_effect=RuntimeError("temporary discord outage"))

    target = {
        "board": "discord-200",
        "thread_id": "200",
        "state": "active",
        "sync_key": "sync-1",
    }
    assert await adapter.sync_kanban_feature_summary(target) is None
    assert await adapter.sync_kanban_feature_summary(target) is None
    assert thread.fetch_message.await_count == 2


@pytest.mark.asyncio
async def test_sync_kanban_feature_summary_circuits_archived_edit_failure(adapter, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    parent = FakeTextChannel(channel_id=100)
    thread = FakeThread(channel_id=200, parent=parent)
    adapter._client.get_channel = lambda channel_id: thread if int(channel_id) == 200 else None

    handle = await adapter.initialize_feature_summary(
        thread,
        parent_channel=parent,
        initial_request="/goal Ship the dashboard",
    )
    assert handle is not None
    handle.pop("_message_obj", None)
    archived_message = SimpleNamespace(id=300, edit=AsyncMock(side_effect=RuntimeError("thread is archived")))
    thread.fetch_message = AsyncMock(return_value=archived_message)

    target = {
        "board": "discord-200",
        "thread_id": "200",
        "state": "active",
        "sync_key": "sync-1",
    }
    assert await adapter.sync_kanban_feature_summary(target) == "sync-1"
    archived_message.edit.assert_awaited_once()

    assert await adapter.sync_kanban_feature_summary(target) == "sync-1"
    archived_message.edit.assert_awaited_once()


@pytest.mark.asyncio
async def test_sync_kanban_feature_summary_circuit_is_scoped_to_message(adapter, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    parent = FakeTextChannel(channel_id=100)
    thread = FakeThread(channel_id=200, parent=parent)
    adapter._client.get_channel = lambda channel_id: thread if int(channel_id) == 200 else None

    handle = await adapter.initialize_feature_summary(
        thread,
        parent_channel=parent,
        initial_request="/goal Ship the dashboard",
    )
    assert handle is not None
    handle.pop("_message_obj", None)
    thread.fetch_message = AsyncMock(side_effect=discord_platform.discord.Forbidden("missing permissions"))

    target = {
        "board": "discord-200",
        "thread_id": "200",
        "state": "active",
        "sync_key": "sync-1",
    }
    assert await adapter.sync_kanban_feature_summary(target) == "sync-1"

    repaired = SimpleNamespace(id=301, edit=AsyncMock())
    thread._messages[301] = repaired
    adapter._persist_feature_summary_handle(
        thread,
        {
            "thread_id": "200",
            "message_id": "301",
            "parent_channel_id": "100",
            "initial_request": "/goal Ship the dashboard",
            "kanban_board": {"slug": "discord-200"},
        },
    )

    async def fetch_repaired(message_id):
        return thread._messages[int(message_id)]

    thread.fetch_message = AsyncMock(side_effect=fetch_repaired)

    assert await adapter.sync_kanban_feature_summary(target) == "sync-1"
    repaired.edit.assert_awaited_once()


@pytest.mark.asyncio
async def test_non_goal_feature_summary_does_not_create_session_kanban_planner(adapter, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    parent = FakeTextChannel(channel_id=100)
    thread = FakeThread(channel_id=200, parent=parent)

    handle = await adapter.initialize_feature_summary(
        thread,
        parent_channel=parent,
        initial_request="Build the drilldown page",
    )

    assert handle is not None
    assert handle["kanban_board"] is None
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db

    board = dwb.board_slug_for_discord_thread("200")
    meta = kanban_db.read_board_metadata(board)
    conn = kanban_db.connect(board=board)
    try:
        tasks = kanban_db.list_tasks(conn, include_archived=False)
    finally:
        conn.close()

    assert "discord_worker" not in meta
    assert tasks == []


@pytest.mark.asyncio
async def test_goal_feature_summary_defers_planner_to_goal_handler(adapter, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    parent = FakeTextChannel(channel_id=100)
    thread = FakeThread(channel_id=200, parent=parent)

    handle = await adapter.initialize_feature_summary(
        thread,
        parent_channel=parent,
        initial_request="/goal Ship the dashboard",
    )

    assert handle is not None
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db

    board = dwb.board_slug_for_discord_thread("200")
    meta = kanban_db.read_board_metadata(board)
    conn = kanban_db.connect(board=board)
    try:
        tasks = kanban_db.list_tasks(conn, include_archived=False)
    finally:
        conn.close()

    assert meta["discord_worker"]["initial_request"] == "/goal Ship the dashboard"
    assert tasks == []


@pytest.mark.asyncio
async def test_goal_feature_summary_with_newline_defers_planner(adapter, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    parent = FakeTextChannel(channel_id=100)
    thread = FakeThread(channel_id=200, parent=parent)

    handle = await adapter.initialize_feature_summary(
        thread,
        parent_channel=parent,
        initial_request="/goal\n\nShip the dashboard",
    )

    assert handle is not None
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db

    board = dwb.board_slug_for_discord_thread("200")
    conn = kanban_db.connect(board=board)
    try:
        tasks = kanban_db.list_tasks(conn, include_archived=False)
    finally:
        conn.close()

    assert tasks == []


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
async def test_tagged_question_answers_in_thread_without_feature_summary(adapter, monkeypatch):
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "true")
    parent = FakeTextChannel(channel_id=100, topic="Existing channel note")
    thread = FakeThread(channel_id=200, parent=parent)
    adapter._auto_create_thread = AsyncMock(return_value=thread)

    await adapter._handle_message(
        _make_message(adapter, channel=parent, content="<@999> What is the repo URL?")
    )

    parent.edit.assert_not_awaited()
    adapter._auto_create_thread.assert_awaited_once()
    assert thread.sent == []
    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.feature_summary is None
    assert event.project_summary is None
    assert event.source.chat_id == "200"
    assert event.source.chat_type == "thread"
    assert event.source.thread_id == "200"
    assert event.source.parent_chat_id == "100"
    assert event.discord_runtime_mode == "read_only"
    assert "default READ-ONLY runtime" in event.channel_prompt


@pytest.mark.asyncio
async def test_auto_threaded_noop_action_label_attaches_feature_summary(adapter, monkeypatch):
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "true")
    parent = FakeTextChannel(channel_id=100, topic="Existing channel note")
    thread = FakeThread(channel_id=200, parent=parent)
    adapter._auto_create_thread = AsyncMock(return_value=thread)
    triage = MagicMock(
        return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="question"))]
        )
    )
    monkeypatch.setattr("agent.auxiliary_client.call_llm", triage)

    await adapter._handle_message(
        _make_message(
            adapter,
            channel=parent,
            content="<@999> no-op change end-to-end",
        )
    )

    triage.assert_not_called()
    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.source.chat_type == "thread"
    assert event.feature_summary is not None
    assert event.feature_summary["initial_request"] == "no-op change end-to-end"
    assert event.feature_summary["thread_id"] == "200"
    assert len(thread.sent) == 1


@pytest.mark.asyncio
async def test_tagged_grill_me_parent_message_threads_without_feature_summary(adapter, monkeypatch):
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "true")
    parent = FakeTextChannel(channel_id=100, topic="Existing channel note")
    thread = FakeThread(channel_id=200, parent=parent)
    adapter._auto_create_thread = AsyncMock(return_value=thread)
    adapter._classify_discord_runtime_mode = AsyncMock(
        return_value=gateway_run.RuntimeMode.ACTION
    )

    await adapter._handle_message(
        _make_message(adapter, channel=parent, content="<@999> please grill me about dashboard auth")
    )

    parent.edit.assert_not_awaited()
    adapter._auto_create_thread.assert_awaited_once()
    adapter._classify_discord_runtime_mode.assert_not_awaited()
    assert thread.sent == []
    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.text == "please grill me about dashboard auth"
    assert event.feature_summary is None
    assert event.project_summary is None
    assert event.source.chat_id == "200"
    assert event.source.chat_type == "thread"
    assert event.source.thread_id == "200"
    assert event.source.parent_chat_id == "100"
    assert event.discord_runtime_mode == "read_only"
    assert "default READ-ONLY runtime" in event.channel_prompt


@pytest.mark.asyncio
async def test_tagged_free_response_question_starts_thread_without_feature_summary(adapter, monkeypatch):
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "true")
    monkeypatch.setenv("DISCORD_FREE_RESPONSE_CHANNELS", "100")
    parent = FakeTextChannel(channel_id=100, topic="Existing channel note")
    thread = FakeThread(channel_id=200, parent=parent)
    adapter._auto_create_thread = AsyncMock(return_value=thread)

    await adapter._handle_message(
        _make_message(adapter, channel=parent, content="<@999> What is the repo URL?")
    )

    parent.edit.assert_not_awaited()
    adapter._auto_create_thread.assert_awaited_once()
    assert thread.sent == []
    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.feature_summary is None
    assert event.project_summary is None
    assert event.source.chat_id == "200"
    assert event.source.chat_type == "thread"
    assert event.discord_runtime_mode == "read_only"
    assert "default READ-ONLY runtime" in event.channel_prompt


@pytest.mark.asyncio
async def test_tagged_reply_question_stays_inline_without_feature_summary(adapter, monkeypatch):
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "true")
    parent = FakeTextChannel(channel_id=100, topic="Existing channel note")
    adapter._auto_create_thread = AsyncMock()
    message = _make_message(adapter, channel=parent, content="<@999> What is the repo URL?")
    message.type = discord_platform.discord.MessageType.reply

    await adapter._handle_message(message)

    parent.edit.assert_not_awaited()
    adapter._auto_create_thread.assert_not_awaited()
    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.feature_summary is None
    assert event.project_summary is None
    assert event.source.chat_id == "100"
    assert event.source.chat_type == "group"
    assert event.discord_runtime_mode == "read_only"
    assert "default READ-ONLY runtime" in event.channel_prompt


@pytest.mark.asyncio
async def test_tagged_priority_change_routes_directly_to_action(adapter, monkeypatch):
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "true")
    parent = FakeTextChannel(channel_id=100, topic="Existing channel note")
    thread = FakeThread(channel_id=200, parent=parent)
    adapter._auto_create_thread = AsyncMock(return_value=thread)
    await adapter._handle_message(
        _make_message(adapter, channel=parent, content="<@999> Change the next priorities to scraper validation")
    )

    parent.edit.assert_awaited()
    adapter._auto_create_thread.assert_awaited_once()
    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.discord_runtime_mode == "action"
    assert event.feature_summary is not None
    assert event.project_summary is not None
    assert event.source.chat_id == "200"
    assert event.source.chat_type == "thread"


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
    assert event.discord_runtime_mode == "read_only"
    assert "default READ-ONLY runtime" in event.channel_prompt


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message_id", "content"),
    [
        (
            1527747538797465641,
            "<@999> Without building anything, can you give me some concrete "
            'examples of the sort of items in the "include only" list?',
        ),
        (
            1535689113758605364,
            "<@999> sorry for the thrash. Let’s say we don’t go with one API. "
            "What were the limitations with the fifty individual states approach?",
        ),
    ],
)
async def test_existing_action_thread_question_keeps_summary_and_uses_read_only(
    adapter,
    monkeypatch,
    message_id,
    content,
):
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "true")
    parent = FakeTextChannel(channel_id=100)
    thread = FakeThread(channel_id=200, parent=parent)
    feature_summary = {
        "thread_id": "200",
        "message_id": "300",
        "initial_request": "Build bounded Federal Register evidence ingestion",
        "kanban_board": None,
    }
    adapter._load_feature_summary_handle_for_thread = MagicMock(return_value=feature_summary)
    adapter.initialize_feature_summary = AsyncMock()

    await adapter._handle_message(
        _make_message(
            adapter,
            channel=thread,
            message_id=message_id,
            content=content,
        )
    )

    adapter.initialize_feature_summary.assert_not_awaited()
    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.message_id == str(message_id)
    assert event.feature_summary is feature_summary
    assert event.discord_runtime_mode == "read_only"
    assert event.discord_action_request_intent is None
    assert event.source.chat_id == "200"
    assert event.source.thread_id == "200"
    assert "default READ-ONLY runtime" in event.channel_prompt


@pytest.mark.asyncio
async def test_mentioned_attachment_only_existing_action_thread_keeps_none_intent(
    adapter,
    monkeypatch,
):
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "true")
    parent = FakeTextChannel(channel_id=100)
    thread = FakeThread(channel_id=200, parent=parent)
    feature_summary = {
        "thread_id": "200",
        "message_id": "300",
        "initial_request": "Build bounded Federal Register evidence ingestion",
        "kanban_board": None,
    }
    adapter._load_feature_summary_handle_for_thread = MagicMock(return_value=feature_summary)
    adapter._classify_discord_runtime_mode = AsyncMock(
        return_value=gateway_run.RuntimeMode.READ_ONLY
    )
    adapter._cache_discord_image = AsyncMock(return_value="/tmp/screenshot.png")
    attachment = FakeAttachment(
        filename="screenshot.png",
        content_type="image/png",
        data=b"png",
    )

    await adapter._handle_message(
        _make_message(
            adapter,
            channel=thread,
            content="<@999>",
            attachments=[attachment],
        )
    )

    adapter._classify_discord_runtime_mode.assert_not_awaited()
    event = adapter.handle_message.await_args.args[0]
    assert event.feature_summary is feature_summary
    assert event.discord_runtime_mode == "action"
    assert event.discord_action_request_intent is None
    assert event.message_type == MessageType.PHOTO
    assert event.media_urls == ["/tmp/screenshot.png"]
    assert "classified as a direct question" not in str(event.channel_prompt or "")


@pytest.mark.asyncio
async def test_reply_attachment_only_existing_action_thread_keeps_none_intent(
    adapter,
    monkeypatch,
):
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "true")
    parent = FakeTextChannel(channel_id=100)
    thread = FakeThread(channel_id=200, parent=parent)
    feature_summary = {
        "thread_id": "200",
        "message_id": "300",
        "initial_request": "Build bounded Federal Register evidence ingestion",
        "kanban_board": None,
    }
    adapter._load_feature_summary_handle_for_thread = MagicMock(return_value=feature_summary)
    adapter._classify_discord_runtime_mode = AsyncMock(
        return_value=gateway_run.RuntimeMode.READ_ONLY
    )
    adapter._cache_discord_document = AsyncMock(return_value=b"pdf")
    attachment = FakeAttachment(
        filename="requirements.pdf",
        content_type="application/pdf",
        data=b"pdf",
    )
    message = _make_message(adapter, channel=thread, content="", attachments=[attachment])
    message.mentions = []
    message.type = discord_platform.discord.MessageType.reply
    message.reference = SimpleNamespace(
        resolved=SimpleNamespace(id=777, author=adapter._client.user, content="Prior response"),
        cached_message=None,
    )

    with patch(
        "plugins.platforms.discord.adapter.cache_document_from_bytes",
        return_value="/tmp/requirements.pdf",
    ):
        await adapter._handle_message(message)

    adapter._classify_discord_runtime_mode.assert_not_awaited()
    event = adapter.handle_message.await_args.args[0]
    assert event.feature_summary is feature_summary
    assert event.discord_runtime_mode == "action"
    assert event.discord_action_request_intent is None
    assert event.message_type == MessageType.DOCUMENT
    assert event.media_urls == ["/tmp/requirements.pdf"]
    assert "classified as a direct question" not in str(event.channel_prompt or "")


@pytest.mark.asyncio
async def test_reply_native_voice_existing_action_thread_classifies_transcript(
    adapter,
    monkeypatch,
):
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "true")
    parent = FakeTextChannel(channel_id=100)
    thread = FakeThread(channel_id=200, parent=parent)
    feature_summary = {
        "thread_id": "200",
        "message_id": "300",
        "initial_request": "Build bounded Federal Register evidence ingestion",
        "kanban_board": None,
    }
    adapter._load_feature_summary_handle_for_thread = MagicMock(return_value=feature_summary)
    adapter._classify_discord_runtime_mode = AsyncMock(
        return_value=gateway_run.RuntimeMode.ACTION
    )
    attachment = SimpleNamespace(
        url="https://cdn.discordapp.com/attachments/fake/voice-message.ogg",
        filename="voice-message.ogg",
        content_type=None,
        size=10,
        read=AsyncMock(return_value=b"fake ogg"),
        duration_secs=2.0,
        waveform=b"fake",
    )
    adapter._preprocess_voice_for_feature_triage = AsyncMock(
        return_value=({id(attachment): ("/tmp/voice.ogg", "audio/ogg")}, "Build the approved parser")
    )
    message = _make_message(adapter, channel=thread, content="", attachments=[attachment])
    message.mentions = []
    message.type = discord_platform.discord.MessageType.reply
    message.reference = SimpleNamespace(
        resolved=SimpleNamespace(id=777, author=adapter._client.user, content="Prior response"),
        cached_message=None,
    )
    message.guild = parent.guild
    message.author.bot = False
    message.flags = SimpleNamespace(voice=True)

    await adapter._handle_message(message)

    adapter._preprocess_voice_for_feature_triage.assert_awaited_once()
    adapter._classify_discord_runtime_mode.assert_awaited_once()
    assert adapter._classify_discord_runtime_mode.await_args.args[0] == "Build the approved parser"
    event = adapter.handle_message.await_args.args[0]
    assert event.feature_summary is not feature_summary
    assert event.feature_summary["source_message_id"] == str(message.id)
    assert event.feature_summary["initial_request"] == "Build the approved parser"
    assert event.discord_runtime_mode == "action"
    assert event.message_type == MessageType.VOICE
    assert event.media_urls == ["/tmp/voice.ogg"]
    assert "classified as a direct question" not in str(event.channel_prompt or "")


@pytest.mark.parametrize(
    ("trigger", "transcript", "expected_mode"),
    [
        ("auto", "Build the approved parser", "action"),
        ("mention", "Migrate the production auth schema", "action"),
        ("reply", "Add parser telemetry", "action"),
        ("action_channel", "Audit the production permission model", "read_only"),
    ],
)
@pytest.mark.asyncio
async def test_existing_thread_native_voice_promotes_action_from_transcript(
    adapter,
    monkeypatch,
    trigger,
    transcript,
    expected_mode,
):
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "true")
    if trigger in {"auto", "action_channel"}:
        monkeypatch.setenv("DISCORD_VOICE_AUTO_TAG", "true")
    if trigger == "action_channel":
        adapter.config.extra["action_request_channels"] = "100"

    parent = FakeTextChannel(channel_id=100)
    thread = FakeThread(channel_id=200, parent=parent)
    adapter._load_feature_summary_handle_for_thread = MagicMock(return_value=None)
    attachment = SimpleNamespace(
        url="https://cdn.discordapp.com/attachments/fake/voice-message.ogg",
        filename="voice-message.ogg",
        content_type=None,
        size=10,
        read=AsyncMock(return_value=b"fake ogg"),
        duration_secs=2.0,
        waveform=b"fake",
    )
    adapter._preprocess_voice_for_feature_triage = AsyncMock(
        return_value=({id(attachment): ("/tmp/voice.ogg", "audio/ogg")}, transcript)
    )
    content = "<@999>" if trigger == "mention" else ""
    message = _make_message(adapter, channel=thread, content=content, attachments=[attachment])
    if trigger != "mention":
        message.mentions = []
    if trigger == "reply":
        message.type = discord_platform.discord.MessageType.reply
        message.reference = SimpleNamespace(
            resolved=SimpleNamespace(id=777, author=adapter._client.user, content="Prior response"),
            cached_message=None,
        )
    message.guild = parent.guild
    message.author.bot = False
    message.flags = SimpleNamespace(voice=True)

    await adapter._handle_message(message)

    adapter._preprocess_voice_for_feature_triage.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.discord_runtime_mode == expected_mode
    if expected_mode == "action":
        assert event.feature_summary["initial_request"] == transcript
        assert gateway_run._is_standard_discord_action_request(event.source, event.feature_summary)
        tier = gateway_run._discord_action_request_model_tier({}, event.feature_summary)
        assert tier is not None
        assert (tier.name, tier.model, tier.reasoning_effort) == (
            "discord_action",
            "gpt-5.6-sol",
            "medium",
        )
        assert any(
            payload.get("content") == f"> {transcript}"
            for payload, _message in thread.sent
        )
    else:
        assert event.feature_summary is None
        assert "default READ-ONLY runtime" in event.channel_prompt
    assert event.media_urls == ["/tmp/voice.ogg"]


@pytest.mark.asyncio
async def test_untriggered_existing_thread_native_voice_is_not_transcribed(adapter, monkeypatch):
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "true")
    monkeypatch.setenv("DISCORD_VOICE_AUTO_TAG", "false")
    parent = FakeTextChannel(channel_id=100)
    thread = FakeThread(channel_id=200, parent=parent)
    attachment = SimpleNamespace(
        filename="voice-message.ogg",
        content_type=None,
        duration_secs=2.0,
        waveform=b"fake",
    )
    adapter._preprocess_voice_for_feature_triage = AsyncMock()
    message = _make_message(adapter, channel=thread, content="", attachments=[attachment])
    message.mentions = []
    message.guild = parent.guild
    message.author.bot = False
    message.flags = SimpleNamespace(voice=True)

    await adapter._handle_message(message)

    adapter._preprocess_voice_for_feature_triage.assert_not_awaited()
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_action_thread_exact_approval_gets_own_summary_without_promotion_prompt(
    adapter,
    monkeypatch,
):
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "true")
    parent = FakeTextChannel(channel_id=100)
    thread = FakeThread(channel_id=200, parent=parent)
    feature_summary = {
        "thread_id": "200",
        "message_id": "300",
        "initial_request": "Build bounded Federal Register evidence ingestion",
        "kanban_board": None,
    }
    adapter._load_feature_summary_handle_for_thread = MagicMock(return_value=feature_summary)
    new_feature_summary = {
        "thread_id": "200",
        "message_id": "301",
        "source_message_id": "1527750909164261467",
        "initial_request": "Okay, let's build this.",
        "kanban_board": None,
    }
    adapter._load_feature_summary_handle_for_request = MagicMock(return_value=None)
    adapter.initialize_feature_summary = AsyncMock(return_value=new_feature_summary)

    await adapter._handle_message(
        _make_message(
            adapter,
            channel=thread,
            message_id=1527750909164261467,
            content="<@999> Okay, let's build this.",
        )
    )

    adapter.initialize_feature_summary.assert_awaited_once()
    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.message_id == "1527750909164261467"
    assert event.feature_summary is new_feature_summary
    assert event.discord_runtime_mode == "action"
    assert "promote_to_action_thread" not in str(event.channel_prompt or "")
    assert "classified as a direct question" not in str(event.channel_prompt or "")


@pytest.mark.parametrize(
    ("content", "expected_mode", "expects_read_only_prompt"),
    [
        ("What changed?", "read_only", True),
        ("Okay, let's build this.", "action", False),
    ],
)
@pytest.mark.asyncio
async def test_unmentioned_existing_action_thread_gets_late_intent_classification(
    adapter,
    monkeypatch,
    content,
    expected_mode,
    expects_read_only_prompt,
):
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "true")
    adapter.config.extra["thread_require_mention"] = False
    parent = FakeTextChannel(channel_id=100)
    thread = FakeThread(channel_id=200, parent=parent)
    adapter._threads.mark("200")
    feature_summary = {
        "thread_id": "200",
        "message_id": "300",
        "initial_request": "Build bounded Federal Register evidence ingestion",
        "kanban_board": None,
    }
    adapter._load_feature_summary_handle_for_thread = MagicMock(return_value=feature_summary)
    message = _make_message(adapter, channel=thread, content=content)
    message.mentions = []
    if expected_mode == "action":
        adapter._load_feature_summary_handle_for_request = MagicMock(return_value=None)
        adapter.initialize_feature_summary = AsyncMock(
            return_value={
                "thread_id": "200",
                "message_id": "301",
                "source_message_id": str(message.id),
                "initial_request": content,
                "kanban_board": None,
            }
        )

    await adapter._handle_message(message)

    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    if expected_mode == "action":
        assert event.feature_summary["message_id"] == "301"
        assert event.feature_summary["source_message_id"] == str(message.id)
    else:
        assert event.feature_summary is feature_summary
    assert event.discord_runtime_mode == expected_mode
    assert (
        "default READ-ONLY runtime" in str(event.channel_prompt or "")
    ) is expects_read_only_prompt


@pytest.mark.parametrize(
    ("attachment", "expected_type", "cached_path"),
    [
        (
            FakeAttachment(filename="screenshot.png", content_type="image/png", data=b"png"),
            MessageType.PHOTO,
            "/tmp/screenshot.png",
        ),
        (
            FakeAttachment(filename="requirements.pdf", content_type="application/pdf", data=b"pdf"),
            MessageType.DOCUMENT,
            "/tmp/requirements.pdf",
        ),
    ],
)
@pytest.mark.asyncio
async def test_unmentioned_attachment_only_action_followup_skips_late_intent_classification(
    adapter,
    monkeypatch,
    attachment,
    expected_type,
    cached_path,
):
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "true")
    adapter.config.extra["thread_require_mention"] = False
    parent = FakeTextChannel(channel_id=100)
    thread = FakeThread(channel_id=200, parent=parent)
    adapter._threads.mark("200")
    feature_summary = {
        "thread_id": "200",
        "message_id": "300",
        "initial_request": "Build bounded Federal Register evidence ingestion",
        "kanban_board": None,
    }
    adapter._load_feature_summary_handle_for_thread = MagicMock(return_value=feature_summary)
    adapter._classify_discord_runtime_mode = AsyncMock(
        return_value=gateway_run.RuntimeMode.READ_ONLY
    )
    adapter._cache_discord_image = AsyncMock(return_value=cached_path)
    adapter._cache_discord_document = AsyncMock(return_value=attachment._data)
    message = _make_message(
        adapter,
        channel=thread,
        content="",
        attachments=[attachment],
    )
    message.mentions = []

    with patch(
        "plugins.platforms.discord.adapter.cache_document_from_bytes",
        return_value=cached_path,
    ):
        await adapter._handle_message(message)

    adapter._classify_discord_runtime_mode.assert_not_awaited()
    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.feature_summary is feature_summary
    assert event.discord_runtime_mode == "action"
    assert event.discord_action_request_intent is None
    assert event.message_type == expected_type
    assert event.media_urls == [cached_path]
    assert "classified as a direct question" not in str(event.channel_prompt or "")


@pytest.mark.asyncio
async def test_unmentioned_voice_action_followup_classifies_transcript(adapter, monkeypatch):
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "true")
    adapter.config.extra["thread_require_mention"] = False
    parent = FakeTextChannel(channel_id=100)
    thread = FakeThread(channel_id=200, parent=parent)
    adapter._threads.mark("200")
    feature_summary = {
        "thread_id": "200",
        "message_id": "300",
        "initial_request": "Build bounded Federal Register evidence ingestion",
        "kanban_board": None,
    }
    adapter._load_feature_summary_handle_for_thread = MagicMock(return_value=feature_summary)
    adapter._classify_discord_runtime_mode = AsyncMock(
        return_value=gateway_run.RuntimeMode.ACTION
    )
    attachment = SimpleNamespace(
        url="https://cdn.discordapp.com/attachments/fake/voice-message.ogg",
        filename="voice-message.ogg",
        content_type=None,
        size=10,
        read=AsyncMock(return_value=b"fake ogg"),
        duration_secs=2.0,
        waveform=b"fake",
    )
    adapter._preprocess_voice_for_feature_triage = AsyncMock(
        return_value=({id(attachment): ("/tmp/voice.ogg", "audio/ogg")}, "Build the approved parser")
    )
    message = _make_message(adapter, channel=thread, content="", attachments=[attachment])
    message.mentions = []
    message.guild = parent.guild
    message.author.bot = False
    message.flags = SimpleNamespace(voice=True)

    await adapter._handle_message(message)

    adapter._classify_discord_runtime_mode.assert_awaited_once()
    assert adapter._classify_discord_runtime_mode.await_args.args[0] == "Build the approved parser"
    event = adapter.handle_message.await_args.args[0]
    assert event.feature_summary is not feature_summary
    assert event.feature_summary["source_message_id"] == str(message.id)
    assert event.feature_summary["initial_request"] == "Build the approved parser"
    assert event.discord_runtime_mode == "action"
    assert event.media_urls == ["/tmp/voice.ogg"]
    assert "classified as a direct question" not in str(event.channel_prompt or "")


@pytest.mark.parametrize(
    "transcript",
    [
        "Build a deploy dashboard",
        "Migrate the production auth schema",
    ],
)
@pytest.mark.asyncio
async def test_native_voice_metadata_without_message_flag_triages_from_transcript(
    adapter,
    monkeypatch,
    transcript,
):
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "true")
    monkeypatch.setenv("DISCORD_VOICE_AUTO_TAG", "true")
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
        type=discord_platform.discord.MessageType.default,
    )

    with patch(
        "plugins.platforms.discord.adapter.cache_audio_from_bytes",
        return_value="/tmp/voice_from_read.ogg",
    ) as mock_cache, patch(
        "tools.transcription_tools.transcribe_audio",
        return_value={"success": True, "transcript": transcript},
    ) as mock_transcribe:
        await adapter._handle_message(message)

    mock_cache.assert_called_once_with(b"fake ogg", ext=".ogg")
    mock_transcribe.assert_called_once_with("/tmp/voice_from_read.ogg")
    adapter._auto_create_thread.assert_awaited_once_with(message)
    assert len(thread.sent) == 2
    assert thread.sent[1][0]["content"] == f"> {transcript}"
    event = adapter.handle_message.await_args.args[0]
    assert event.feature_summary["thread_id"] == "200"
    assert event.feature_summary["initial_request"] == transcript
    tier = gateway_run._discord_action_request_model_tier({}, event.feature_summary)
    assert tier is not None
    assert (tier.name, tier.model, tier.reasoning_effort) == (
        "discord_action",
        "gpt-5.6-sol",
        "medium",
    )
    assert event.media_urls == ["/tmp/voice_from_read.ogg"]
    assert event.media_types == ["audio/ogg"]


def test_feature_summary_transcript_quote_sanitizes_mass_mentions(adapter):
    quote = adapter._format_feature_summary_transcript_quote(
        "Please alert @everyone\nand @here"
    )

    assert quote == "> Please alert @\u200beveryone\n> and @\u200bhere"


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


def test_successful_rollout_ledger_status_maps_to_complete_reaction(adapter, tmp_path):
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.work_ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="200",
        chat_type="thread",
        thread_id="200",
        message_id="400",
    )
    source.project_path = "/home/droid/workspaces/pid-action"
    source.project_github_url = "https://github.com/sligo-labs/PID"
    event = MessageEvent(
        text="Proceed with the same treatment across the other sources",
        source=source,
        message_id="400",
    )
    item = runner.work_ledger.accept_event(event, session_key="discord:200", freshness_seconds=60)
    assert item is not None
    runner.work_ledger.mark_agent_done(
        item["id"],
        final_response=(
            "77 items audited, 52 published, and 25 intentionally held by quality gates. "
            "Production deployment passed. Airflow runtime is current, clean, zero behind, and has "
            "no sensitive lag. The protected canonical checkout remains untouched because it has a "
            "pre-existing deleted test and is 16 commits behind. The active worktree and production "
            "runtime are current."
        ),
        summary_status="Complete",
    )

    status = runner._discord_ledger_summary_status(
        item["id"],
        runner._discord_summary_status({"exit_reason": "text_response"}),
    )

    assert status == "Complete"
    assert adapter._summary_status_reaction_emoji(status) == "✅"


def test_delivered_terminal_summary_is_not_reopened_by_pending_closeout():
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.work_ledger = SimpleNamespace(
        get=lambda _work_id: {
            "status": "summary_updated",
            "summary_status": "Complete",
            "closeout_authoritative": True,
            "closeout": {
                "mode": "enforce",
                "status": "waiting_for_preview",
                "policy": {"require_preview": True},
            },
        }
    )

    assert runner._discord_ledger_summary_status("work-1", "Complete") == "Complete"


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


def test_runner_persists_discord_kanban_fallback_title(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from hermes_cli import discord_worker_boards as dwb

    board = dwb.set_goal(thread_id="200", goal="Ship the deploy dashboard")
    runner = object.__new__(gateway_run.GatewayRunner)

    title = runner._discord_kanban_feature_title(
        {
            "board": board.slug,
            "fallback_title": "Ship the deploy dashboard",
            "outcome": "In progress. Planner created implementation tickets.",
        }
    )

    assert title == "Ship the deploy dashboard"
    assert dwb.feature_summary_snapshot(board.slug)["title"] == "Ship the deploy dashboard"


@pytest.mark.asyncio
async def test_discord_feature_summary_title_uses_request_not_stale_session_title():
    runner = object.__new__(gateway_run.GatewayRunner)
    runner._session_db = SimpleNamespace(get_session_title=lambda _session_id: "Unrelated stale title")
    adapter = SimpleNamespace(update_feature_summary=AsyncMock())
    runner.adapters = {Platform.DISCORD: adapter}
    source = SessionSource(platform=Platform.DISCORD, chat_id="200", chat_type="thread")
    feature_summary = {
        "message_id": "300",
        "initial_request": "investigate and fix thread 1507755696501030933",
        "kanban_board": None,
    }

    assert await runner._update_discord_summaries(
        source=source,
        feature_summary=feature_summary,
        final_response="Fixed.",
        status="Complete",
        session_id="session-1",
    )

    adapter.update_feature_summary.assert_awaited_once_with(
        feature_summary,
        final_response="Fixed.",
        status="Complete",
        title="investigate and fix thread 1507755696501030933",
    )


@pytest.mark.asyncio
async def test_terminal_feature_summary_collects_transient_session_artifacts(monkeypatch):
    from hermes_cli import plugins as plugin_api

    artifacts = [
        {
            "kind": "external_url",
            "label": "Execution trace",
            "url": "https://artifacts.example.test/runs/abc",
        }
    ]
    collect = MagicMock(return_value=artifacts)
    monkeypatch.setattr(plugin_api, "collect_session_artifacts", collect)
    runner = object.__new__(gateway_run.GatewayRunner)
    runner._session_db = None
    adapter = SimpleNamespace(update_feature_summary=AsyncMock(return_value=True))
    runner.adapters = {Platform.DISCORD: adapter}
    source = SessionSource(platform=Platform.DISCORD, chat_id="200", chat_type="thread")
    feature_summary = {
        "message_id": "300",
        "initial_request": "Ship the trace links",
        "kanban_board": None,
    }
    original_summary = dict(feature_summary)

    assert await runner._update_discord_summaries(
        source=source,
        feature_summary=feature_summary,
        final_response="Shipped.",
        status="Blocked: awaiting operator approval",
        session_id="session-1",
    )

    collect.assert_called_once_with(
        "session-1",
        surface="discord_feature_summary",
    )
    adapter.update_feature_summary.assert_awaited_once_with(
        feature_summary,
        final_response="Shipped.",
        status="Blocked: awaiting operator approval",
        title="Ship the trace links",
        artifacts=artifacts,
    )
    assert feature_summary == original_summary
    assert "artifacts" not in feature_summary


@pytest.mark.asyncio
async def test_running_feature_summary_collects_session_artifacts(monkeypatch):
    from hermes_cli import plugins as plugin_api

    artifacts = [
        {
            "kind": "external_url",
            "label": "Agent Trace",
            "url": "https://artifacts.example.test/runs/live",
        }
    ]
    collect = MagicMock(return_value=artifacts)
    monkeypatch.setattr(plugin_api, "collect_session_artifacts", collect)
    runner = object.__new__(gateway_run.GatewayRunner)
    runner._session_db = None
    adapter = SimpleNamespace(update_feature_summary=AsyncMock(return_value=True))
    runner.adapters = {Platform.DISCORD: adapter}
    source = SessionSource(platform=Platform.DISCORD, chat_id="200", chat_type="thread")

    assert await runner._update_discord_summaries(
        source=source,
        feature_summary={"message_id": "300", "initial_request": "Keep working"},
        final_response="Still working.",
        status="Running",
        session_id="session-1",
    )

    collect.assert_called_once_with(
        "session-1",
        surface="discord_feature_summary",
    )
    adapter.update_feature_summary.assert_awaited_once_with(
        {"message_id": "300", "initial_request": "Keep working"},
        final_response="Still working.",
        status="Running",
        title="Keep working",
        artifacts=artifacts,
    )


@pytest.mark.asyncio
async def test_resolved_session_refreshes_running_feature_summary_artifacts(monkeypatch):
    from hermes_cli import plugins as plugin_api

    artifacts = [
        {
            "kind": "external_url",
            "label": "Agent Trace",
            "url": "https://artifacts.example.test/runs/live",
        }
    ]
    collect = MagicMock(return_value=artifacts)
    monkeypatch.setattr(plugin_api, "collect_session_artifacts", collect)
    runner = object.__new__(gateway_run.GatewayRunner)
    adapter = SimpleNamespace(update_feature_summary=AsyncMock(return_value=True))
    runner.adapters = {Platform.DISCORD: adapter}
    source = SessionSource(platform=Platform.DISCORD, chat_id="200", chat_type="thread")
    feature_summary = {"message_id": "300", "initial_request": "Keep working"}

    assert await runner._refresh_discord_running_summary_artifacts(
        source=source,
        feature_summary=feature_summary,
        session_id="session-1",
    )

    collect.assert_called_once_with(
        "session-1",
        surface="discord_feature_summary",
    )
    adapter.update_feature_summary.assert_awaited_once_with(
        feature_summary,
        status="Running",
        artifacts=artifacts,
    )


@pytest.mark.asyncio
async def test_feature_summary_artifact_provider_failure_is_fail_open(monkeypatch):
    from hermes_cli import plugins as plugin_api

    collect = MagicMock(side_effect=RuntimeError("provider failed"))
    monkeypatch.setattr(plugin_api, "collect_session_artifacts", collect)
    runner = object.__new__(gateway_run.GatewayRunner)
    runner._session_db = None
    adapter = SimpleNamespace(update_feature_summary=AsyncMock(return_value=True))
    runner.adapters = {Platform.DISCORD: adapter}
    source = SessionSource(platform=Platform.DISCORD, chat_id="200", chat_type="thread")

    assert await runner._update_discord_summaries(
        source=source,
        feature_summary={"message_id": "300", "initial_request": "Ship it"},
        final_response="Done.",
        status="Complete",
        session_id="session-1",
    )

    collect.assert_called_once()
    adapter.update_feature_summary.assert_awaited_once()
    assert "artifacts" not in adapter.update_feature_summary.await_args.kwargs


def test_feature_summary_renders_only_safe_artifact_links_between_runtime_and_kanban(adapter):
    valid = {
        "kind": "external_url",
        "label": "Execution trace",
        "url": "https://artifacts.example.test/runs/abc(1)",
    }
    artifacts = [
        None,
        {"kind": "trace", "label": "Wrong kind", "url": "https://example.test/run"},
        {"kind": "external_url", "label": "Credentials", "url": "https://u:p@example.test/run"},
        {"kind": "external_url", "label": "Whitespace", "url": "https://example.test/a b"},
        {"kind": "external_url", "label": "Control", "url": "https://example.test/a\n"},
        {"kind": "external_url", "label": "Scheme", "url": "ftp://example.test/a"},
        {"kind": "external_url", "label": "Bad port", "url": "https://example.test:99999/a"},
        {
            "kind": "external_url",
            "label": "Overlong",
            "url": "https://example.test/" + "a" * 1000,
        },
        valid,
        dict(valid),
    ]

    embed = adapter._build_feature_summary_embed(
        initial_request="Ship it",
        status="Complete",
        outcome="Done",
        title="Artifact links",
        metadata={"branch": None, "pr_url": None},
        runtime_breakdown={"wall_s": 12},
        artifacts=artifacts,
        kanban_url="https://kanban.example.test/workers/200",
    )

    field_names = [field.name for field in embed.fields]
    assert field_names.index("Time Spent") < field_names.index("Execution trace")
    assert field_names.index("Execution trace") < field_names.index("Kanban Board")
    assert field_names.count("Execution trace") == 1
    fields = {field.name: field.value for field in embed.fields}
    assert fields["Execution trace"] == (
        "[Open link](https://artifacts.example.test/runs/abc%281%29)"
    )
    assert "Wrong kind" not in fields
    assert "Credentials" not in fields
    assert "Whitespace" not in fields
    assert "Control" not in fields
    assert "Scheme" not in fields
    assert "Bad port" not in fields
    assert "Overlong" not in fields
    assert not fields["Execution trace"].endswith("...")


@pytest.mark.asyncio
async def test_direct_question_updates_reactions_without_touching_action_lifecycle(adapter):
    feature_summary = {
        "thread_id": "200",
        "message_id": "300",
        "initial_request": "Implement the parser",
        "title": "Parser rollout",
        "status": "In progress",
        "outcome": "Implementation is halfway complete.",
        "kanban_board": None,
    }
    before = dict(feature_summary)
    event = MessageEvent(
        text="What remains unfinished?",
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id="200",
            chat_type="thread",
            thread_id="200",
        ),
        feature_summary=feature_summary,
        discord_action_request_intent=False,
    )
    adapter._reactions_enabled = MagicMock(return_value=True)
    adapter.update_feature_summary = AsyncMock(return_value=True)
    messages = [
        SimpleNamespace(id=400, add_reaction=AsyncMock()),
        SimpleNamespace(id=300, add_reaction=AsyncMock()),
    ]
    adapter._processing_reaction_messages = AsyncMock(return_value=messages)
    adapter._set_message_reaction_state = AsyncMock()

    await adapter.on_processing_start(event)
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

    adapter.update_feature_summary.assert_not_awaited()
    assert adapter._processing_reaction_messages.await_count == 2
    assert [call.args for call in adapter._set_message_reaction_state.await_args_list] == [
        (messages[0], "⏳"),
        (messages[1], "⏳"),
        (messages[0], "✅"),
        (messages[1], "✅"),
    ]
    assert feature_summary == before


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
        discord_runtime_mode="action",
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


@pytest.mark.asyncio
async def test_discord_summary_callback_completes_work_ledger_after_embed_update(tmp_path):
    runner = object.__new__(gateway_run.GatewayRunner)
    runner._session_db = None
    runner.work_ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    adapter = SimpleNamespace(
        callbacks=[],
        update_feature_summary=AsyncMock(return_value=True),
    )

    def register_post_delivery_callback(session_key, callback, generation=None):
        adapter.callbacks.append((session_key, callback, generation))

    adapter.register_post_delivery_callback = register_post_delivery_callback
    runner.adapters = {Platform.DISCORD: adapter}
    source = SessionSource(platform=Platform.DISCORD, chat_id="200", chat_type="thread")
    event = MessageEvent(
        text="Build it",
        source=source,
        message_id="400",
        feature_summary={"message_id": "300"},
        discord_runtime_mode="action",
    )
    item = runner.work_ledger.accept_event(event, session_key="discord:200", freshness_seconds=60)
    assert item is not None
    event.work_item_id = item["id"]
    runner.work_ledger.mark_agent_done(item["id"], final_response="Final answer")
    runner.work_ledger.mark_response_delivered(item["id"], result_message_id="500")

    runner._register_discord_summary_post_delivery(
        event=event,
        source=source,
        session_key="discord:200",
        run_generation=7,
        session_id="session-1",
        final_response="Final answer",
        agent_result={"completed": True},
    )
    result = adapter.callbacks[0][1]()
    if inspect.isawaitable(result):
        result = await result

    assert result is True
    assert runner.work_ledger.get(item["id"])["status"] == "completed"


@pytest.mark.asyncio
async def test_discord_summary_callback_leaves_work_incomplete_when_embed_update_fails(tmp_path):
    runner = object.__new__(gateway_run.GatewayRunner)
    runner._session_db = None
    runner.work_ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    adapter = SimpleNamespace(
        callbacks=[],
        update_feature_summary=AsyncMock(return_value=False),
    )

    def register_post_delivery_callback(session_key, callback, generation=None):
        adapter.callbacks.append((session_key, callback, generation))

    adapter.register_post_delivery_callback = register_post_delivery_callback
    runner.adapters = {Platform.DISCORD: adapter}
    source = SessionSource(platform=Platform.DISCORD, chat_id="200", chat_type="thread")
    event = MessageEvent(
        text="Build it",
        source=source,
        message_id="400",
        feature_summary={"message_id": "300"},
    )
    item = runner.work_ledger.accept_event(event, session_key="discord:200", freshness_seconds=60)
    assert item is not None
    event.work_item_id = item["id"]
    runner.work_ledger.mark_agent_done(item["id"], final_response="Final answer")
    runner.work_ledger.mark_response_delivered(item["id"], result_message_id="500")

    runner._register_discord_summary_post_delivery(
        event=event,
        source=source,
        session_key="discord:200",
        run_generation=7,
        session_id="session-1",
        final_response="Final answer",
        agent_result={"completed": True},
    )
    result = adapter.callbacks[0][1]()
    if inspect.isawaitable(result):
        result = await result

    assert result is False
    assert runner.work_ledger.get(item["id"])["status"] == "response_delivered"
