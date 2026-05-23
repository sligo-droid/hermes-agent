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

    async def send(self, chat_id, content, metadata=None):
        self.sent.append({"chat_id": chat_id, "content": content, "metadata": metadata})
        if self.error:
            raise self.error
        return self.result


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


def test_foreman_defaults_are_disabled_with_expected_discord_target():
    foreman = DEFAULT_CONFIG["kanban"]["discord_worker"]["foreman"]

    assert foreman["enabled"] is False
    assert foreman["channel_id"] == "1504252294495998043"
    assert foreman["mention"] == "<@1504235933598486580>"


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

    adapter = ForemanAdapter(result=SimpleNamespace(success=False, error="token=abc /tmp/private.log"))
    asyncio.run(_run_one_foreman_tick(monkeypatch, _runner(adapter)))

    assert sent == []
    assert failed == [("t1", "token=[redacted] [path]")]


def test_foreman_watcher_suppresses_old_terminal_issues(monkeypatch):
    _patch_config(monkeypatch, _enabled_config(terminal_suppression_age_seconds=3600))
    _patch_lock(monkeypatch)
    monkeypatch.setattr("gateway.run.time.time", lambda: 10_000)
    from hermes_cli import discord_worker_foreman as foreman

    old = _issue("old", run_ended_at=1)
    recent = _issue("recent", run_ended_at=9_000)
    due_inputs = []

    monkeypatch.setattr(foreman, "collect_foreman_issues", lambda now=None: [old, recent])
    monkeypatch.setattr(foreman, "alerts_due", lambda issues, *, config=None, now=None: due_inputs.extend(issues) or [])

    asyncio.run(_run_one_foreman_tick(monkeypatch, _runner(ForemanAdapter())))

    assert [issue.task_id for issue in due_inputs] == ["recent"]


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
