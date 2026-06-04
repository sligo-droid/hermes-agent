import asyncio
import time
from types import SimpleNamespace

from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli.config import DEFAULT_CONFIG


class ForemanAdapter:
    def __init__(self, *, connected=True, result=None, error=None):
        self.is_connected = connected
        self.result = result
        self.error = error
        self.sent = []
        self.created_threads = []
        self.created_goals = []
        self.synced_reactions = []

    async def send(self, chat_id, content, metadata=None):
        self.sent.append({"chat_id": chat_id, "content": content, "metadata": metadata})
        if self.error:
            raise self.error
        return self.result

    async def create_worker_task_thread(self, parent_chat_id, **kwargs):
        self.created_threads.append({"parent_chat_id": parent_chat_id, **kwargs})
        return {
            "thread_id": f"thread-{len(self.created_threads)}",
            "thread_name": kwargs.get("name") or "worker task",
            "message_id": f"message-{len(self.created_threads)}",
        }

    async def send_worker_task_embed(self, thread_chat_id, **kwargs):
        self.created_threads.append({"thread_chat_id": thread_chat_id, **kwargs})
        return {
            "thread_id": str(thread_chat_id),
            "thread_name": kwargs.get("title") or "worker task",
            "message_id": f"message-{len(self.created_threads)}",
        }

    async def create_foreman_goal_thread(self, parent_chat_id, **kwargs):
        self.created_goals.append({"parent_chat_id": parent_chat_id, **kwargs})
        if self.error:
            raise self.error
        return {
            "thread_id": str(parent_chat_id),
            "thread_name": kwargs.get("name") or "foreman goal",
            "message_id": f"goal-message-{len(self.created_goals)}",
            "guild_id": "guild-1",
            "parent_channel_id": "source-parent",
            "initial_request": kwargs.get("initial_request") or "",
            "project_context": {"project_name": "Hermes", "project_path": "/repo/hermes"},
        }

    async def sync_kanban_thread_reaction(self, target):
        self.synced_reactions.append(dict(target))
        return target.get("reaction_state") or target.get("state")


def _runner(adapter=None):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.DISCORD: adapter} if adapter else {}
    return runner


async def _run_one_foreman_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._discord_worker_foreman_watcher()


def _enabled_config(**overrides):
    foreman = {
        "enabled": True,
        "channel_id": "foreman-channel",
        "mention": "@foreman",
        "scan_interval_seconds": 1,
        "cooldown_seconds": 1,
        "retry_backoff_seconds": 1,
        "max_retry_backoff_seconds": 1,
        "retention_seconds": 1,
        "max_alerts_per_tick": 500,
        "max_alerts_per_board_per_day": 500,
        "terminal_suppression_age_seconds": 0,
    }
    foreman.update(overrides)
    return {"kanban": {"discord_worker": {"foreman": foreman}}}


def _issue(task_id="t1", **evidence):
    return SimpleNamespace(
        kind="worker_errored",
        board="discord-1",
        task_id=task_id,
        severity="error",
        title="Worker failed",
        evidence={"task_status": "blocked", **evidence},
    )


def _human_issue(task_id="t1", **evidence):
    return SimpleNamespace(
        kind="human_intervention_required",
        board="discord-1",
        task_id=task_id,
        severity="critical",
        title="Foreman attempt requires human manual intervention",
        evidence={"task_status": "blocked", **evidence},
    )


def _patch_lock(monkeypatch):
    import gateway.status as status

    monkeypatch.setattr(status, "acquire_scoped_lock", lambda *a, **k: (True, None))
    monkeypatch.setattr(status, "release_scoped_lock", lambda *a, **k: None)


def _patch_config(monkeypatch, config):
    import hermes_cli.config as cfg

    monkeypatch.setattr(cfg, "load_config", lambda: config)


def _patch_no_human_escalations(monkeypatch):
    from hermes_cli import discord_worker_foreman as foreman

    monkeypatch.setattr(foreman, "auto_close_completed_foreman_boards", lambda **kwargs: [])
    monkeypatch.setattr(foreman, "collect_human_intervention_issues", lambda **kwargs: [])


def test_foreman_defaults_are_disabled_with_expected_discord_target():
    foreman = DEFAULT_CONFIG["kanban"]["discord_worker"]["foreman"]

    assert foreman["enabled"] is False
    assert foreman["channel_id"] == "1504252294495998043"
    assert foreman["mention"] == "<@&1503914570077442058>"
    assert foreman["master_board"] == "default"
    assert foreman["blocked_board_min_age_seconds"] == 600


def test_foreman_watcher_missing_config_does_not_scan(monkeypatch):
    _patch_config(monkeypatch, {"kanban": {"discord_worker": {}}})
    _patch_lock(monkeypatch)
    from hermes_cli import discord_worker_foreman as foreman

    monkeypatch.setattr(
        foreman,
        "collect_foreman_issues",
        lambda now=None, **kwargs: (_ for _ in ()).throw(AssertionError("must not scan")),
    )

    adapter = ForemanAdapter()
    asyncio.run(_run_one_foreman_tick(monkeypatch, _runner(adapter)))

    assert adapter.sent == []
    assert adapter.created_goals == []


def test_foreman_spawned_task_records_thread_state_without_transition_subscription(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    monkeypatch.setenv("HERMES_PUBLIC_KANBAN_BASE_URL", "https://hermes.example.test")
    _patch_config(monkeypatch, _enabled_config())

    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_state import read_codex_worker_state

    board = dwb.ensure_discord_thread_board(
        thread_id="123",
        chat_id="parent-123",
        guild_id="guild-1",
        parent_channel_id="dev-parent",
        initial_request="/goal Ship the dashboard",
        project_context={"project_name": "Hermes", "project_path": "/repo/hermes"},
    )
    conn = kanban_db.connect(board=board.slug)
    try:
        task_id = kanban_db.create_task(
            conn,
            title="Build dashboard filters",
            body="Add filter controls and verify them.",
            assignee=dwb.ROLE_DEV,
            tenant=board.slug,
            initial_status="running",
            board=board.slug,
        )
    finally:
        conn.close()

    adapter = ForemanAdapter()
    runner = _runner(adapter)
    result = SimpleNamespace(spawned=[(task_id, dwb.ROLE_DEV, "/tmp/hermes-worktree")])

    asyncio.run(runner._discord_foreman_announce_spawned_tasks([(board.slug, result)]))
    asyncio.run(runner._discord_foreman_announce_spawned_tasks([(board.slug, result)]))

    assert adapter.created_threads == []

    state = read_codex_worker_state(task_id, board=board.slug)
    assert state["foreman_thread"]["thread_id"] == "123"
    assert state["foreman_thread"]["message_id"] == ""

    conn = kanban_db.connect(board=board.slug)
    try:
        subs = kanban_db.list_notify_subs(conn, task_id)
    finally:
        conn.close()
    assert subs == []


def test_foreman_watcher_disabled_config_does_not_scan(monkeypatch):
    _patch_config(monkeypatch, _enabled_config(enabled=False))
    _patch_lock(monkeypatch)
    from hermes_cli import discord_worker_foreman as foreman

    monkeypatch.setattr(
        foreman,
        "collect_foreman_issues",
        lambda now=None, **kwargs: (_ for _ in ()).throw(AssertionError("must not scan")),
    )

    adapter = ForemanAdapter()
    asyncio.run(_run_one_foreman_tick(monkeypatch, _runner(adapter)))

    assert adapter.sent == []


def test_foreman_watcher_requires_discord_adapter_and_connection(monkeypatch):
    _patch_config(monkeypatch, _enabled_config())
    _patch_lock(monkeypatch)
    from hermes_cli import discord_worker_foreman as foreman

    monkeypatch.setattr(
        foreman,
        "collect_foreman_issues",
        lambda now=None, **kwargs: (_ for _ in ()).throw(AssertionError("must not scan")),
    )

    asyncio.run(_run_one_foreman_tick(monkeypatch, _runner()))

    disconnected = ForemanAdapter(connected=False)
    asyncio.run(_run_one_foreman_tick(monkeypatch, _runner(disconnected)))

    assert disconnected.sent == []


def test_foreman_watcher_enqueues_due_issue_on_master_board(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    _patch_config(monkeypatch, _enabled_config())
    _patch_lock(monkeypatch)
    _patch_no_human_escalations(monkeypatch)
    from hermes_cli import discord_worker_foreman as foreman
    from hermes_cli import kanban_db

    first = _issue(
        "t1",
        thread_id="source-thread-1",
        chat_id="source-thread-1",
        parent_channel_id="source-parent",
    )
    skipped = _issue("t2", thread_id="source-thread-2", chat_id="source-thread-2")
    due_calls = []
    sent = []
    failed = []

    monkeypatch.setattr(foreman, "collect_foreman_issues", lambda now=None, **kwargs: [first, skipped])
    monkeypatch.setattr(foreman, "startup_baseline_needed", lambda: False)

    def fake_alerts_due(issues, *, config=None, now=None):
        due_calls.append((issues, config, now))
        return [first]

    monkeypatch.setattr(foreman, "alerts_due", fake_alerts_due)
    monkeypatch.setattr(foreman, "record_alert_sent", lambda issue: sent.append(issue.task_id))
    monkeypatch.setattr(foreman, "record_alert_failed", lambda issue, error: failed.append((issue.task_id, error)))

    adapter = ForemanAdapter()
    runner = _runner(adapter)
    asyncio.run(_run_one_foreman_tick(monkeypatch, runner))

    assert [issue.task_id for issue in due_calls[0][0]] == ["t1", "t2"]
    assert due_calls[0][1]["max_alerts_per_tick"] == 50
    assert due_calls[0][1]["daily_cap_per_board"] == 200
    assert due_calls[0][1]["terminal_suppression_age_seconds"] == 0
    assert adapter.sent == []
    assert adapter.created_goals == []
    conn = kanban_db.connect(board="default")
    try:
        tasks = kanban_db.list_tasks(conn, include_archived=False)
    finally:
        conn.close()
    assert len(tasks) == 1
    task = tasks[0]
    assert task.created_by == "discord-worker-foreman"
    assert task.status == "ready"
    assert task.tenant == "discord-1"
    assert task.idempotency_key.startswith("discord-foreman:discord-1:t1:worker_errored:")
    assert "create child tickets on this same master board" in (task.body or "")
    assert adapter.synced_reactions == [
        {
            "board": "discord-1",
            "thread_id": "source-thread-1",
            "chat_id": "source-thread-1",
            "message_id": "",
            "source_message_id": "",
            "state": "active",
            "reaction_state": "foreman",
        }
    ]
    assert sent == ["t1"]
    assert failed == []


def test_foreman_watcher_direct_alerts_human_intervention_issue(monkeypatch):
    _patch_config(monkeypatch, _enabled_config())
    _patch_lock(monkeypatch)
    from hermes_cli import discord_worker_foreman as foreman

    issue = _human_issue(
        "source-task",
        source_board="discord-source",
        thread_id="source-thread",
        chat_id="source-thread",
        guild_id="guild-1",
        source_message_id="source-message",
        foreman_board="discord-foreman",
        manual_intervention_reason="Human must create the API key.",
    )
    sent = []
    failed = []

    monkeypatch.setattr(foreman, "auto_close_completed_foreman_boards", lambda **kwargs: [])
    monkeypatch.setattr(foreman, "collect_foreman_issues", lambda now=None, **kwargs: [])
    monkeypatch.setattr(foreman, "collect_human_intervention_issues", lambda now=None, **kwargs: [issue])
    monkeypatch.setattr(foreman, "startup_baseline_needed", lambda: False)
    monkeypatch.setattr(foreman, "alerts_due", lambda issues, *, config=None, now=None: list(issues))
    rendered_mentions = []
    monkeypatch.setattr(
        foreman,
        "render_foreman_alert",
        lambda issue, mention="": rendered_mentions.append(mention) or f"{mention}\nhuman alert: {issue.task_id}",
    )
    embed_issues = []
    monkeypatch.setattr(
        foreman,
        "render_foreman_human_intervention_embed",
        lambda issue: embed_issues.append(issue) or {"title": "Foreman needs human input"},
    )
    monkeypatch.setattr(foreman, "record_alert_sent", lambda issue: sent.append(issue.task_id))
    monkeypatch.setattr(foreman, "record_alert_failed", lambda issue, error: failed.append((issue.task_id, error)))

    adapter = ForemanAdapter()
    adapter.supports_metadata_embeds = True
    runner = _runner(adapter)
    goal_events = []

    async def fake_handle_goal(event):
        goal_events.append(event)
        return None

    monkeypatch.setattr(runner, "_handle_goal_command", fake_handle_goal)
    asyncio.run(_run_one_foreman_tick(monkeypatch, runner))

    assert adapter.sent == [
        {
            "chat_id": "source-thread",
            "content": "<@&1503914570077442058>",
            "metadata": {
                "foreman_alert_kind": "human_intervention_required",
                "allowed_role_mentions": ["1503914570077442058"],
                "_discord_embed": {"title": "Foreman needs human input"},
            },
        }
    ]
    assert adapter.created_goals == []
    assert goal_events == []
    assert adapter.synced_reactions == [
        {
            "board": "discord-source",
                "thread_id": "source-thread",
                "chat_id": "source-thread",
                "message_id": "",
                "source_message_id": "source-message",
                "state": "active",
                "reaction_state": "foreman",
            }
    ]
    assert rendered_mentions == ["<@&1503914570077442058>"]
    assert embed_issues[0].evidence["source_discord_thread_url"] == (
        "https://discord.com/channels/guild-1/source-thread/source-message"
    )
    assert sent == ["source-task"]
    assert failed == []


def test_foreman_watcher_suppresses_matching_goal_when_human_alert_is_due(monkeypatch):
    _patch_config(monkeypatch, _enabled_config())
    _patch_lock(monkeypatch)
    from hermes_cli import discord_worker_foreman as foreman

    source_issue = _issue(
        "source-task",
        source_board="discord-source",
        source_task_id="source-task",
        source_issue_kind="worker_errored",
        thread_id="source-thread",
        chat_id="source-thread",
    )
    human_issue = _human_issue(
        "source-task",
        source_board="discord-source",
        source_task_id="source-task",
        source_issue_kind="worker_errored",
        thread_id="source-thread",
        chat_id="source-thread",
        foreman_board="discord-foreman",
        manual_intervention_reason="Human must create the API key.",
    )
    sent = []
    failed = []

    monkeypatch.setattr(foreman, "collect_foreman_issues", lambda now=None, **kwargs: [source_issue])
    monkeypatch.setattr(foreman, "collect_human_intervention_issues", lambda now=None, **kwargs: [human_issue])
    monkeypatch.setattr(foreman, "startup_baseline_needed", lambda: False)
    monkeypatch.setattr(foreman, "alerts_due", lambda issues, *, config=None, now=None: list(issues))
    monkeypatch.setattr(foreman, "render_foreman_alert", lambda issue, mention="": f"human alert: {issue.task_id}")
    monkeypatch.setattr(foreman, "render_foreman_goal_prompt", lambda issue: "/goal Fix worker")
    monkeypatch.setattr(foreman, "foreman_goal_thread_title", lambda issue: "Foreman worker")
    monkeypatch.setattr(foreman, "record_alert_sent", lambda issue: sent.append(issue.kind))
    monkeypatch.setattr(foreman, "record_alert_failed", lambda issue, error: failed.append((issue.kind, error)))

    adapter = ForemanAdapter()
    runner = _runner(adapter)
    goal_events = []

    async def fake_handle_goal(event):
        goal_events.append(event)
        return None

    monkeypatch.setattr(runner, "_handle_goal_command", fake_handle_goal)
    asyncio.run(_run_one_foreman_tick(monkeypatch, runner))

    assert adapter.sent == [
        {
            "chat_id": "source-thread",
            "content": "human alert: source-task",
            "metadata": {
                "foreman_alert_kind": "human_intervention_required",
                "allowed_role_mentions": ["1503914570077442058"],
            },
        }
    ]
    assert adapter.created_goals == []
    assert goal_events == []
    assert sent == ["human_intervention_required"]
    assert failed == []


def test_foreman_watcher_auto_closes_before_collecting_issues(monkeypatch):
    _patch_config(monkeypatch, _enabled_config())
    _patch_lock(monkeypatch)
    from hermes_cli import discord_worker_foreman as foreman

    calls = []

    def auto_close(now=None):
        calls.append(("auto", now))
        return [{"foreman_board": "foreman-1"}]

    def collect_foreman(now=None, **kwargs):
        calls.append(("foreman", now))
        return []

    def collect_human(now=None, **kwargs):
        calls.append(("human", now))
        return []

    monkeypatch.setattr(foreman, "auto_close_completed_foreman_boards", auto_close)
    monkeypatch.setattr(foreman, "collect_foreman_issues", collect_foreman)
    monkeypatch.setattr(foreman, "collect_human_intervention_issues", collect_human)
    monkeypatch.setattr(foreman, "startup_baseline_needed", lambda: False)
    monkeypatch.setattr(foreman, "alerts_due", lambda issues, *, config=None, now=None: [])

    asyncio.run(_run_one_foreman_tick(monkeypatch, _runner(ForemanAdapter())))

    assert [name for name, _ in calls] == ["auto", "foreman", "human"]
    assert len({now for _, now in calls}) == 1


def test_foreman_watcher_records_send_result_failure(monkeypatch):
    _patch_config(monkeypatch, _enabled_config())
    _patch_lock(monkeypatch)
    _patch_no_human_escalations(monkeypatch)
    from hermes_cli import discord_worker_foreman as foreman

    issue = _issue("t1", thread_id="source-thread")
    sent = []
    failed = []

    monkeypatch.setattr(foreman, "collect_foreman_issues", lambda now=None, **kwargs: [issue])
    monkeypatch.setattr(foreman, "startup_baseline_needed", lambda: False)
    monkeypatch.setattr(foreman, "alerts_due", lambda issues, *, config=None, now=None: list(issues))
    monkeypatch.setattr(
        foreman,
        "create_foreman_master_task",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("Authorization: Bearer abc123 ghp_abcdefghijklmnopqrst ~/.hermes/config.yaml")
        ),
    )
    monkeypatch.setattr(foreman, "record_alert_sent", lambda issue: sent.append(issue.task_id))
    monkeypatch.setattr(foreman, "record_alert_failed", lambda issue, error: failed.append((issue.task_id, error)))

    adapter = ForemanAdapter()
    asyncio.run(_run_one_foreman_tick(monkeypatch, _runner(adapter)))

    assert sent == []
    assert failed == [("t1", "[redacted] [redacted] [path]")]


def test_foreman_watcher_delegates_terminal_suppression_to_alerts_due(monkeypatch):
    _patch_config(monkeypatch, _enabled_config(terminal_suppression_age_seconds=3600))
    _patch_lock(monkeypatch)
    _patch_no_human_escalations(monkeypatch)
    monkeypatch.setattr("gateway.run.time.time", lambda: 10_000)
    from hermes_cli import discord_worker_foreman as foreman

    old = _issue("old", run_ended_at=1)
    recent = _issue("recent", run_ended_at=9_000)
    due_inputs = []
    due_configs = []

    monkeypatch.setattr(foreman, "collect_foreman_issues", lambda now=None, **kwargs: [old, recent])
    monkeypatch.setattr(foreman, "startup_baseline_needed", lambda: False)
    monkeypatch.setattr(
        foreman,
        "alerts_due",
        lambda issues, *, config=None, now=None: due_configs.append(config) or due_inputs.extend(issues) or [],
    )

    asyncio.run(_run_one_foreman_tick(monkeypatch, _runner(ForemanAdapter())))

    assert [issue.task_id for issue in due_inputs] == ["old", "recent"]
    assert due_configs[0]["terminal_suppression_age_seconds"] == 3600


def test_foreman_watcher_baselines_existing_startup_issues(monkeypatch):
    _patch_config(monkeypatch, _enabled_config())
    _patch_lock(monkeypatch)
    _patch_no_human_escalations(monkeypatch)
    from hermes_cli import discord_worker_foreman as foreman

    issue = _issue("historical")
    baselined = []

    monkeypatch.setattr(foreman, "collect_foreman_issues", lambda now=None, **kwargs: [issue])
    monkeypatch.setattr(foreman, "startup_baseline_needed", lambda: True)
    monkeypatch.setattr(
        foreman,
        "record_startup_baseline",
        lambda issues, *, config=None, now=None: baselined.extend(issues) or len(issues),
    )
    monkeypatch.setattr(
        foreman,
        "alerts_due",
        lambda issues, *, config=None, now=None: (_ for _ in ()).throw(AssertionError("must not alert")),
    )

    adapter = ForemanAdapter()
    asyncio.run(_run_one_foreman_tick(monkeypatch, _runner(adapter)))

    assert [item.task_id for item in baselined] == ["historical"]
    assert adapter.sent == []
    assert adapter.created_goals == []


def test_foreman_watcher_active_guard_prevents_duplicate_runner_loop(monkeypatch):
    _patch_config(monkeypatch, _enabled_config())
    from hermes_cli import discord_worker_foreman as foreman

    monkeypatch.setattr(
        foreman,
        "collect_foreman_issues",
        lambda now=None, **kwargs: (_ for _ in ()).throw(AssertionError("must not scan")),
    )
    runner = _runner(ForemanAdapter())
    runner._discord_worker_foreman_watcher_active = True

    asyncio.run(runner._discord_worker_foreman_watcher())

    assert runner.adapters[Platform.DISCORD].sent == []


def test_foreman_dirty_marker_wakes_sleep_before_full_interval(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))

    async def run():
        from hermes_cli import discord_worker_boards as dwb

        runner = _runner(ForemanAdapter())
        runner._kanban_foreman_dirty_marker_ns = dwb.dispatch_dirty_marker_mtime_ns()

        async def mark_soon():
            await asyncio.sleep(0.05)
            dwb.mark_dispatch_dirty(board="discord-1", reason="self-improvement-approved")

        marker_task = asyncio.create_task(mark_soon())
        started = time.monotonic()
        woke = await runner._sleep_until_discord_worker_dirty_or_timeout(
            5.0,
            marker_state_attr="_kanban_foreman_dirty_marker_ns",
        )
        elapsed = time.monotonic() - started
        await marker_task

        assert woke is True
        assert elapsed < 2.0

    asyncio.run(run())


def test_dispatcher_and_foreman_dirty_marker_state_do_not_interfere(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))

    async def run():
        from hermes_cli import discord_worker_boards as dwb

        runner = _runner(ForemanAdapter())
        current_marker = dwb.dispatch_dirty_marker_mtime_ns()
        runner._kanban_dispatch_dirty_marker_ns = current_marker
        runner._kanban_foreman_dirty_marker_ns = current_marker

        dwb.mark_dispatch_dirty(board="discord-1", reason="self-improvement-approved")

        assert await runner._sleep_until_kanban_dispatch_due(0) is True
        assert await runner._sleep_until_discord_worker_dirty_or_timeout(
            0,
            marker_state_attr="_kanban_foreman_dirty_marker_ns",
        ) is True
        assert runner._kanban_dispatch_dirty_marker_ns == runner._kanban_foreman_dirty_marker_ns

    asyncio.run(run())
