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


def _patch_lock(monkeypatch):
    import gateway.status as status

    monkeypatch.setattr(status, "acquire_scoped_lock", lambda *a, **k: (True, None))
    monkeypatch.setattr(status, "release_scoped_lock", lambda *a, **k: None)


def _patch_config(monkeypatch, config):
    import hermes_cli.config as cfg

    monkeypatch.setattr(cfg, "load_config", lambda: config)


def test_foreman_defaults_are_enabled_with_expected_discord_target():
    foreman = DEFAULT_CONFIG["kanban"]["discord_worker"]["foreman"]

    assert foreman["enabled"] is True
    assert foreman["channel_id"] == "1504252294495998043"
    assert foreman["mention"] == "<@1504235933598486580>"


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
        lambda now=None: (_ for _ in ()).throw(AssertionError("must not scan")),
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
        lambda now=None: (_ for _ in ()).throw(AssertionError("must not scan")),
    )

    asyncio.run(_run_one_foreman_tick(monkeypatch, _runner()))

    disconnected = ForemanAdapter(connected=False)
    asyncio.run(_run_one_foreman_tick(monkeypatch, _runner(disconnected)))

    assert disconnected.sent == []


def test_foreman_watcher_sends_due_alert_to_fixed_channel_and_mention(monkeypatch):
    _patch_config(monkeypatch, _enabled_config())
    _patch_lock(monkeypatch)
    from hermes_cli import discord_worker_foreman as foreman

    first = _issue("t1")
    skipped = _issue("t2")
    due_calls = []
    sent = []
    failed = []

    monkeypatch.setattr(foreman, "collect_foreman_issues", lambda now=None: [first, skipped])

    def fake_alerts_due(issues, *, config=None, now=None):
        due_calls.append((issues, config, now))
        return [first]

    monkeypatch.setattr(foreman, "alerts_due", fake_alerts_due)
    monkeypatch.setattr(foreman, "render_foreman_alert", lambda issue, mention="": f"{mention} {issue.task_id}")
    monkeypatch.setattr(foreman, "record_alert_sent", lambda issue: sent.append(issue.task_id))
    monkeypatch.setattr(foreman, "record_alert_failed", lambda issue, error: failed.append((issue.task_id, error)))

    adapter = ForemanAdapter()
    asyncio.run(_run_one_foreman_tick(monkeypatch, _runner(adapter)))

    assert [issue.task_id for issue in due_calls[0][0]] == ["t1", "t2"]
    assert due_calls[0][1]["max_alerts_per_tick"] == 50
    assert due_calls[0][1]["daily_cap_per_board"] == 200
    assert due_calls[0][1]["terminal_suppression_age_seconds"] == 0
    assert adapter.sent == [
        {
            "chat_id": "1504252294495998043",
            "content": "<@1504235933598486580> t1",
            "metadata": None,
        }
    ]
    assert sent == ["t1"]
    assert failed == []


def test_foreman_watcher_records_send_result_failure(monkeypatch):
    _patch_config(monkeypatch, _enabled_config())
    _patch_lock(monkeypatch)
    from hermes_cli import discord_worker_foreman as foreman

    issue = _issue("t1")
    sent = []
    failed = []

    monkeypatch.setattr(foreman, "collect_foreman_issues", lambda now=None: [issue])
    monkeypatch.setattr(foreman, "alerts_due", lambda issues, *, config=None, now=None: list(issues))
    monkeypatch.setattr(foreman, "render_foreman_alert", lambda issue, mention="": "alert")
    monkeypatch.setattr(foreman, "record_alert_sent", lambda issue: sent.append(issue.task_id))
    monkeypatch.setattr(foreman, "record_alert_failed", lambda issue, error: failed.append((issue.task_id, error)))

    adapter = ForemanAdapter(
        result=SimpleNamespace(
            success=False,
            error="Authorization: Bearer abc123 ghp_abcdefghijklmnopqrst ~/.hermes/config.yaml",
        )
    )
    asyncio.run(_run_one_foreman_tick(monkeypatch, _runner(adapter)))

    assert sent == []
    assert failed == [("t1", "[redacted] [redacted] [path]")]


def test_foreman_watcher_delegates_terminal_suppression_to_alerts_due(monkeypatch):
    _patch_config(monkeypatch, _enabled_config(terminal_suppression_age_seconds=3600))
    _patch_lock(monkeypatch)
    monkeypatch.setattr("gateway.run.time.time", lambda: 10_000)
    from hermes_cli import discord_worker_foreman as foreman

    old = _issue("old", run_ended_at=1)
    recent = _issue("recent", run_ended_at=9_000)
    due_inputs = []
    due_configs = []

    monkeypatch.setattr(foreman, "collect_foreman_issues", lambda now=None: [old, recent])
    monkeypatch.setattr(
        foreman,
        "alerts_due",
        lambda issues, *, config=None, now=None: due_configs.append(config) or due_inputs.extend(issues) or [],
    )

    asyncio.run(_run_one_foreman_tick(monkeypatch, _runner(ForemanAdapter())))

    assert [issue.task_id for issue in due_inputs] == ["old", "recent"]
    assert due_configs[0]["terminal_suppression_age_seconds"] == 3600


def test_foreman_watcher_active_guard_prevents_duplicate_runner_loop(monkeypatch):
    _patch_config(monkeypatch, _enabled_config())
    from hermes_cli import discord_worker_foreman as foreman

    monkeypatch.setattr(
        foreman,
        "collect_foreman_issues",
        lambda now=None: (_ for _ in ()).throw(AssertionError("must not scan")),
    )
    runner = _runner(ForemanAdapter())
    runner._discord_worker_foreman_watcher_active = True

    asyncio.run(runner._discord_worker_foreman_watcher())

    assert runner.adapters[Platform.DISCORD].sent == []
