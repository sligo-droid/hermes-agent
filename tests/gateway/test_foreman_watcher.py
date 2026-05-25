import asyncio
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

    async def create_foreman_goal_thread(self, parent_chat_id, **kwargs):
        self.created_goals.append({"parent_chat_id": parent_chat_id, **kwargs})
        if self.error:
            raise self.error
        return {
            "thread_id": f"goal-thread-{len(self.created_goals)}",
            "thread_name": kwargs.get("name") or "foreman goal",
            "message_id": f"goal-message-{len(self.created_goals)}",
            "guild_id": "guild-1",
            "parent_channel_id": str(parent_chat_id),
            "initial_request": kwargs.get("initial_request") or "",
            "project_context": {"project_name": "Hermes", "project_path": "/repo/hermes"},
        }


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

    monkeypatch.setattr(foreman, "collect_human_intervention_issues", lambda **kwargs: [])


def test_foreman_defaults_are_disabled_with_expected_discord_target():
    foreman = DEFAULT_CONFIG["kanban"]["discord_worker"]["foreman"]

    assert foreman["enabled"] is False
    assert foreman["channel_id"] == "1504252294495998043"
    assert foreman["mention"] == "<@&1503914570077442058>"
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


def test_foreman_spawned_task_creates_dev_thread_and_subscription(monkeypatch, tmp_path):
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

    assert len(adapter.created_threads) == 1
    created = adapter.created_threads[0]
    assert created["parent_chat_id"] == "1504252294495998043"
    assert created["name"] == "dev: Build dashboard filters"
    assert created["title"] == "Build dashboard filters"
    assert "Add filter controls" in created["initial_request"]
    assert created["project_context"] == {"project_name": "Hermes", "project_path": "/repo/hermes"}
    assert created["kanban_url"] == f"https://hermes.example.test/workers/123/tickets/{task_id}"

    state = read_codex_worker_state(task_id, board=board.slug)
    assert state["foreman_thread"]["thread_id"] == "thread-1"
    assert state["foreman_thread"]["message_id"] == "message-1"

    conn = kanban_db.connect(board=board.slug)
    try:
        subs = kanban_db.list_notify_subs(conn, task_id)
    finally:
        conn.close()
    assert subs == [
        {
            "task_id": task_id,
            "platform": "discord",
            "chat_id": "1504252294495998043",
            "thread_id": "thread-1",
            "user_id": "system:foreman",
            "notifier_profile": "default",
            "created_at": subs[0]["created_at"],
            "last_event_id": 0,
        }
    ]


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


def test_foreman_watcher_starts_due_issue_as_internal_goal(monkeypatch):
    _patch_config(monkeypatch, _enabled_config())
    _patch_lock(monkeypatch)
    _patch_no_human_escalations(monkeypatch)
    from hermes_cli import discord_worker_foreman as foreman

    first = _issue("t1")
    skipped = _issue("t2")
    due_calls = []
    sent = []
    failed = []

    monkeypatch.setattr(foreman, "collect_foreman_issues", lambda now=None, **kwargs: [first, skipped])
    monkeypatch.setattr(foreman, "startup_baseline_needed", lambda: False)

    def fake_alerts_due(issues, *, config=None, now=None):
        due_calls.append((issues, config, now))
        return [first]

    monkeypatch.setattr(foreman, "alerts_due", fake_alerts_due)
    monkeypatch.setattr(foreman, "render_foreman_goal_prompt", lambda issue: f"/goal Fix {issue.task_id}")
    monkeypatch.setattr(foreman, "foreman_goal_thread_title", lambda issue: f"Foreman {issue.task_id}")
    monkeypatch.setattr(foreman, "record_alert_sent", lambda issue: sent.append(issue.task_id))
    monkeypatch.setattr(foreman, "record_alert_failed", lambda issue, error: failed.append((issue.task_id, error)))

    adapter = ForemanAdapter()
    runner = _runner(adapter)
    goal_events = []

    async def fake_handle_goal(event):
        goal_events.append(event)
        return None

    monkeypatch.setattr(runner, "_handle_goal_command", fake_handle_goal)
    asyncio.run(_run_one_foreman_tick(monkeypatch, runner))

    assert [issue.task_id for issue in due_calls[0][0]] == ["t1", "t2"]
    assert due_calls[0][1]["max_alerts_per_tick"] == 50
    assert due_calls[0][1]["daily_cap_per_board"] == 200
    assert due_calls[0][1]["terminal_suppression_age_seconds"] == 0
    assert adapter.sent == []
    assert adapter.created_goals == [
        {
            "parent_chat_id": "1504252294495998043",
            "name": "Foreman t1",
            "initial_request": "/goal Fix t1",
            "project_context": None,
        }
    ]
    assert len(goal_events) == 1
    event = goal_events[0]
    assert event.text == "/goal Fix t1"
    assert event.internal is True
    assert event.source.thread_id == "goal-thread-1"
    assert event.source.parent_chat_id == "1504252294495998043"
    assert event.source.user_id == "system:foreman"
    assert event.feature_summary["message_id"] == "goal-message-1"
    assert sent == ["t1"]
    assert failed == []


def test_foreman_watcher_direct_alerts_human_intervention_issue(monkeypatch):
    _patch_config(monkeypatch, _enabled_config())
    _patch_lock(monkeypatch)
    from hermes_cli import discord_worker_foreman as foreman

    issue = _human_issue(
        "source-task",
        source_board="discord-source",
        foreman_board="discord-foreman",
        manual_intervention_reason="Human must create the API key.",
    )
    sent = []
    failed = []

    monkeypatch.setattr(foreman, "collect_foreman_issues", lambda now=None, **kwargs: [])
    monkeypatch.setattr(foreman, "collect_human_intervention_issues", lambda now=None, **kwargs: [issue])
    monkeypatch.setattr(foreman, "startup_baseline_needed", lambda: False)
    monkeypatch.setattr(foreman, "alerts_due", lambda issues, *, config=None, now=None: list(issues))
    rendered_mentions = []
    monkeypatch.setattr(
        foreman,
        "render_foreman_alert",
        lambda issue, mention="": rendered_mentions.append(mention) or f"human alert: {issue.task_id}",
    )
    monkeypatch.setattr(foreman, "record_alert_sent", lambda issue: sent.append(issue.task_id))
    monkeypatch.setattr(foreman, "record_alert_failed", lambda issue, error: failed.append((issue.task_id, error)))

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
            "chat_id": "1504252294495998043",
            "content": "human alert: source-task",
            "metadata": {
                "foreman_alert_kind": "human_intervention_required",
                "allowed_role_mentions": ["1503914570077442058"],
            },
        }
    ]
    assert adapter.created_goals == []
    assert goal_events == []
    assert rendered_mentions == ["<@&1503914570077442058>"]
    assert sent == ["source-task"]
    assert failed == []


def test_foreman_watcher_records_send_result_failure(monkeypatch):
    _patch_config(monkeypatch, _enabled_config())
    _patch_lock(monkeypatch)
    _patch_no_human_escalations(monkeypatch)
    from hermes_cli import discord_worker_foreman as foreman

    issue = _issue("t1")
    sent = []
    failed = []

    monkeypatch.setattr(foreman, "collect_foreman_issues", lambda now=None, **kwargs: [issue])
    monkeypatch.setattr(foreman, "startup_baseline_needed", lambda: False)
    monkeypatch.setattr(foreman, "alerts_due", lambda issues, *, config=None, now=None: list(issues))
    monkeypatch.setattr(foreman, "render_foreman_goal_prompt", lambda issue: "/goal Fix worker")
    monkeypatch.setattr(foreman, "foreman_goal_thread_title", lambda issue: "Foreman worker")
    monkeypatch.setattr(foreman, "record_alert_sent", lambda issue: sent.append(issue.task_id))
    monkeypatch.setattr(foreman, "record_alert_failed", lambda issue, error: failed.append((issue.task_id, error)))

    adapter = ForemanAdapter(
        error=RuntimeError("Authorization: Bearer abc123 ghp_abcdefghijklmnopqrst ~/.hermes/config.yaml")
    )
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
