import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock


from gateway.config import Platform
from gateway.run import (
    GatewayRunner,
    _format_gateway_flow_telemetry,
    _gateway_flow_route_type,
    _gateway_flow_telemetry_fields,
)
from hermes_cli import kanban_db as kb


class RecordingAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})


class TypingAdapter:
    def __init__(self):
        self.typing = []

    async def send_typing_once(self, chat_id, metadata=None):
        self.typing.append({"chat_id": chat_id, "metadata": metadata or {}})


class FeatureSummarySyncAdapter:
    def __init__(self):
        self.synced = []

    async def sync_kanban_feature_summary(self, target):
        self.synced.append(dict(target))
        return target.get("sync_key") or target.get("board")


class ReactionSyncAdapter:
    def __init__(self):
        self.synced = []

    async def sync_kanban_thread_reaction(self, target):
        self.synced.append(dict(target))
        return target.get("state")


class DiscordStatusSyncAdapter(FeatureSummarySyncAdapter, ReactionSyncAdapter):
    pass


class CompletionNoticeAdapter(DiscordStatusSyncAdapter):
    def __init__(self):
        super().__init__()
        self.completions = []

    async def send_kanban_completion_notice(self, target):
        self.completions.append(dict(target))
        from hermes_cli import discord_worker_boards as dwb

        dwb.mark_thread_status_synced(str(target.get("board") or ""), completion_message=True)
        return target.get("board")


class DisconnectedAdapters(dict):
    """Expose a platform during collection, then simulate disconnect on get()."""

    def get(self, key, default=None):
        return None


async def _run_one_notifier_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


async def _run_one_discord_typing_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._discord_kanban_typing_watcher(interval=1)


def _make_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    return runner


def _make_discord_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.DISCORD: adapter}
    return runner


def test_gateway_flow_telemetry_formats_ids_and_durations_only():
    source = SimpleNamespace(
        platform=Platform.DISCORD,
        chat_id="chat-1",
        thread_id="thread-2",
        user_id="user-3",
    )

    fields = _gateway_flow_telemetry_fields(
        route_type="discord_worker_goal",
        source=source,
        session_id="session-4",
        admission_ts=10.0,
        dispatch_start_ts=10.25,
        finished_ts=11.0,
    )
    line = _format_gateway_flow_telemetry(fields)

    assert _gateway_flow_route_type(SimpleNamespace(text="hello"), None) == "mainline"
    assert _gateway_flow_route_type(SimpleNamespace(text="/goal ship"), "goal") == "slash_goal"
    assert "route_type=discord_worker_goal" in line
    assert "platform=discord" in line
    assert "chat_id=chat-1" in line
    assert "thread_id=thread-2" in line
    assert "user_id=user-3" in line
    assert "session_id=session-4" in line
    assert "admission_to_dispatch_ms=250" in line
    assert "dispatch_to_finish_ms=750" in line
    assert "ship" not in line


def test_kanban_dispatch_dirty_signal_wakes_sleep():
    async def run():
        runner = GatewayRunner.__new__(GatewayRunner)
        assert runner._signal_kanban_dispatcher_dirty() is True
        assert runner._kanban_dispatch_dirty_event.is_set()
        woke = await runner._sleep_until_kanban_dispatch_due(1.0)
        assert woke is True
        assert not runner._kanban_dispatch_dirty_event.is_set()

    asyncio.run(run())


def test_discord_worker_dirty_marker_wakes_dispatch_sleep(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))

    async def run():
        from hermes_cli import discord_worker_boards as dwb

        runner = GatewayRunner.__new__(GatewayRunner)
        runner._running = True
        runner._kanban_dispatch_dirty_marker_ns = dwb.dispatch_dirty_marker_mtime_ns()
        dwb.mark_dispatch_dirty(board="discord-1", reason="test")
        woke = await runner._sleep_until_kanban_dispatch_due(1.0)
        assert woke is True

    asyncio.run(run())


def _create_completed_subscription(summary="done once"):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="notify once", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.complete_task(conn, tid, summary=summary)
        return tid
    finally:
        conn.close()


def _unseen_terminal_events(tid):
    conn = kb.connect()
    try:
        _, events = kb.unseen_events_for_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            kinds=["completed", "blocked", "gave_up", "crashed", "timed_out"],
        )
        return events
    finally:
        conn.close()


def test_kanban_notifier_dedupes_board_slugs_pointing_to_same_db(tmp_path, monkeypatch):
    db_path = tmp_path / "shared-kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    kb.write_board_metadata("alias-a", name="Alias A")
    kb.write_board_metadata("alias-b", name="Alias B")

    tid = _create_completed_subscription()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert "Kanban" in adapter.sent[0]["text"]
    assert tid in adapter.sent[0]["text"]


def test_discord_kanban_typing_watcher_pulses_running_thread(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from hermes_cli import discord_worker_boards as dwb

    board = dwb.set_goal(
        thread_id="99001",
        goal="Show Discord typing while active",
        chat_id="parent-990",
    )
    conn = kb.connect(board=board.slug)
    try:
        tid = kb.create_task(
            conn,
            title="Active dev task",
            assignee=dwb.ROLE_DEV,
            tenant=board.slug,
        )
        kb.claim_task(conn, tid)
    finally:
        conn.close()

    adapter = TypingAdapter()
    runner = _make_discord_runner(adapter)

    asyncio.run(_run_one_discord_typing_tick(monkeypatch, runner))

    assert adapter.typing == [
        {
            "chat_id": "parent-990",
            "metadata": {"thread_id": "99001"},
        }
    ]

    targets = dwb.thread_status_targets()
    assert [target["board"] for target in targets] == [board.slug]
    assert targets[0]["state"] == "running"


def test_discord_kanban_typing_watcher_syncs_feature_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from hermes_cli import discord_worker_boards as dwb

    board = dwb.set_goal(
        thread_id="99002",
        goal="Sync the feature summary card",
        chat_id="parent-991",
        guild_id="guild-991",
        parent_channel_id="parent-991",
    )

    adapter = FeatureSummarySyncAdapter()
    runner = _make_discord_runner(adapter)

    asyncio.run(_run_one_discord_typing_tick(monkeypatch, runner))

    assert len(adapter.synced) == 1
    target = adapter.synced[0]
    assert target["board"] == board.slug
    assert target["thread_id"] == "99002"
    assert target["guild_id"] == "guild-991"
    assert target["parent_channel_id"] == "parent-991"
    assert target["state"] == "active"
    assert target["fallback_title"] == "Sync the feature summary card"
    assert target["outcome"]
    assert target["sync_key"]


def test_discord_kanban_typing_watcher_suppresses_repeated_summary_sync(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from hermes_cli import discord_worker_boards as dwb

    dwb.set_goal(
        thread_id="99003",
        goal="Sync the feature summary card once",
        chat_id="parent-992",
    )

    adapter = FeatureSummarySyncAdapter()
    runner = _make_discord_runner(adapter)

    asyncio.run(_run_one_discord_typing_tick(monkeypatch, runner))
    runner._running = True
    asyncio.run(_run_one_discord_typing_tick(monkeypatch, runner))

    assert len(adapter.synced) == 1


def test_discord_kanban_target_cache_key_includes_thread_identity():
    runner = GatewayRunner.__new__(GatewayRunner)

    first = runner._discord_kanban_target_cache_key(
        {
            "board": "discord-99002",
            "thread_id": "99002",
            "guild_id": "guild-1",
            "parent_channel_id": "parent-1",
        }
    )
    second = runner._discord_kanban_target_cache_key(
        {
            "board": "discord-99002",
            "thread_id": "99002",
            "guild_id": "guild-2",
            "parent_channel_id": "parent-1",
        }
    )

    assert first != second


def test_discord_kanban_typing_watcher_skips_completed_status_sync(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from hermes_cli import discord_worker_boards as dwb

    board = dwb.set_goal(
        thread_id="99007",
        goal="Do not touch completed Discord thread",
        chat_id="parent-996",
    )
    conn = kb.connect(board=board.slug)
    try:
        task = kb.list_tasks(conn, include_archived=False)[0]
        claimed = kb.claim_task(conn, task.id)
        assert claimed is not None
        kb.complete_task(conn, task.id, summary="done", expected_run_id=claimed.current_run_id)
    finally:
        conn.close()
    dwb._update_worker_meta(board.slug, {"goal_status": "done", "phase": "complete"})
    dwb.mark_thread_status_synced(board.slug, reaction=True, summary=True)

    adapter = DiscordStatusSyncAdapter()
    runner = _make_discord_runner(adapter)

    asyncio.run(_run_one_discord_typing_tick(monkeypatch, runner))

    assert adapter.synced == []


def test_discord_status_targets_keep_source_thread_hammer_during_active_foreman(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from hermes_cli import discord_worker_boards as dwb

    source = dwb.set_goal(
        thread_id="99020",
        goal="Original worker request",
        chat_id="parent-99020",
    )
    conn = kb.connect(board=source.slug)
    try:
        task = kb.list_tasks(conn, include_archived=False)[0]
        claimed = kb.claim_task(conn, task.id)
        assert claimed is not None
        kb.complete_task(conn, task.id, summary="done", expected_run_id=claimed.current_run_id)
    finally:
        conn.close()
    dwb._update_worker_meta(source.slug, {"goal_status": "done", "phase": "complete"})

    foreman = dwb.set_goal(
        thread_id="99021",
        goal=f"/goal Foreman escalation: resolve worker issue\n- Board: {source.slug}",
        chat_id="parent-99021",
    )

    targets = dwb.thread_status_targets()
    by_board = {target["board"]: target for target in targets}

    assert by_board[source.slug]["state"] == "done"
    assert by_board[source.slug]["reaction_state"] == "foreman"
    assert by_board[foreman.slug]["hide_source_links"] is True


def test_discord_status_targets_keep_source_thread_hammer_during_master_foreman_task(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli.discord_worker_foreman import ForemanIssue, create_foreman_master_task

    source = dwb.set_goal(
        thread_id="99022",
        goal="Original worker request",
        chat_id="parent-99022",
    )
    conn = kb.connect(board=source.slug)
    try:
        task = kb.list_tasks(conn, include_archived=False)[0]
        claimed = kb.claim_task(conn, task.id)
        assert claimed is not None
        kb.complete_task(conn, task.id, summary="done", expected_run_id=claimed.current_run_id)
    finally:
        conn.close()
    dwb._update_worker_meta(source.slug, {"goal_status": "done", "phase": "complete"})

    create_foreman_master_task(
        ForemanIssue(
            kind="worker_errored",
            board=source.slug,
            task_id="source-task",
            severity="error",
            title="Worker execution failed",
            evidence={"thread_id": "99022", "task_status": "blocked"},
        ),
        master_board="default",
        assignee="default",
    )

    targets = dwb.thread_status_targets()
    by_board = {target["board"]: target for target in targets}

    assert by_board[source.slug]["state"] == "done"
    assert by_board[source.slug]["reaction_state"] == "foreman"


def test_discord_terminal_status_target_syncs_only_when_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from hermes_cli import discord_worker_boards as dwb

    board = dwb.set_goal(
        thread_id="99017",
        goal="Sync final emoji once",
        chat_id="parent-99017",
    )
    conn = kb.connect(board=board.slug)
    try:
        task = kb.list_tasks(conn, include_archived=False)[0]
        claimed = kb.claim_task(conn, task.id)
        assert claimed is not None
        kb.complete_task(conn, task.id, summary="done", expected_run_id=claimed.current_run_id)
    finally:
        conn.close()
    dwb._update_worker_meta(
        board.slug,
        {
            "goal_status": "done",
            "phase": "complete",
            "terminal_reaction_sync_pending": True,
            "terminal_summary_sync_pending": True,
        },
    )

    targets = dwb.thread_status_targets()
    assert [target["board"] for target in targets] == [board.slug]
    assert targets[0]["state"] == "done"

    dwb.mark_thread_status_synced(board.slug, reaction=True)
    assert [target["board"] for target in dwb.thread_status_targets()] == [board.slug]

    dwb.mark_thread_status_synced(board.slug, summary=True)
    assert dwb.thread_status_targets() == []


def test_discord_terminal_pr_refresh_requeues_summary_sync(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from hermes_cli import discord_worker_boards as dwb

    board = dwb.set_goal(
        thread_id="99023",
        goal="Refresh merged PR status",
        chat_id="parent-99023",
    )
    conn = kb.connect(board=board.slug)
    try:
        task = kb.list_tasks(conn, include_archived=False)[0]
        claimed = kb.claim_task(conn, task.id)
        assert claimed is not None
        kb.complete_task(conn, task.id, summary="done", expected_run_id=claimed.current_run_id)
    finally:
        conn.close()
    dwb._update_worker_meta(
        board.slug,
        {
            "goal_status": "done",
            "phase": "complete",
            "pr_url": "https://github.com/acme/hermes/pull/185",
            "pr_checks_status": "pending",
            "pr_checks_total": 2,
        },
    )
    dwb.mark_thread_status_synced(board.slug, reaction=True, summary=True)
    assert dwb.thread_status_targets() == []

    dwb._update_worker_meta(
        board.slug,
        {
            "pr_checks_status": "passed",
            "pr_merge_state": "MERGED",
            "pr_state": "merged",
            "pr_merged_at": "2026-06-01T17:35:13Z",
            "pr_merge_commit": "abc123",
        },
    )

    targets = dwb.thread_status_targets()
    assert [target["board"] for target in targets] == [board.slug]
    assert targets[0]["state"] == "done"
    assert targets[0]["terminal_summary_sync_pending"] is True
    assert targets[0]["terminal_reaction_sync_pending"] is False

    worker = kb.read_board_metadata(board.slug)["discord_worker"]
    assert worker["terminal_summary_sync_pending"] is True
    assert worker["board_summary"]["pr"]["checks_status"] == "passed"
    assert worker["board_summary"]["pr"]["merge_state"] == "MERGED"
    assert worker["board_summary"]["pr"]["merge_commit"] == "abc123"


def test_discord_stale_completion_notice_flag_keeps_terminal_target_until_cleared(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from hermes_cli import discord_worker_boards as dwb

    board = dwb.set_goal(
        thread_id="99018",
        goal="Clear a stale completion notice flag",
        chat_id="parent-99018",
    )
    conn = kb.connect(board=board.slug)
    try:
        task = kb.list_tasks(conn, include_archived=False)[0]
        claimed = kb.claim_task(conn, task.id)
        assert claimed is not None
        kb.complete_task(conn, task.id, summary="done", expected_run_id=claimed.current_run_id)
    finally:
        conn.close()
    dwb._update_worker_meta(
        board.slug,
        {
            "goal_status": "done",
            "phase": "complete",
            "terminal_reaction_sync_pending": True,
            "terminal_summary_sync_pending": True,
            "terminal_completion_message_pending": True,
        },
    )

    target = dwb.thread_status_targets()[0]
    assert target["terminal_completion_message_pending"] is True

    dwb.mark_thread_status_synced(board.slug, reaction=True, summary=True)
    target = dwb.thread_status_targets()[0]
    assert target["terminal_completion_message_pending"] is True

    dwb.mark_thread_status_synced(board.slug, completion_message=True)
    assert dwb.thread_status_targets() == []
    worker = kb.read_board_metadata(board.slug)["discord_worker"]
    assert "terminal_completion_message_pending" not in worker
    assert "terminal_completion_message_sent_at" not in worker


def test_discord_kanban_typing_watcher_clears_stale_completion_notice_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from hermes_cli import discord_worker_boards as dwb

    board = dwb.set_goal(
        thread_id="99019",
        goal="Suppress completed goal notice",
        chat_id="parent-99019",
    )
    conn = kb.connect(board=board.slug)
    try:
        task = kb.list_tasks(conn, include_archived=False)[0]
        claimed = kb.claim_task(conn, task.id)
        assert claimed is not None
        kb.complete_task(conn, task.id, summary="done", expected_run_id=claimed.current_run_id)
    finally:
        conn.close()
    dwb._update_worker_meta(
        board.slug,
        {
            "goal_status": "done",
            "phase": "complete",
            "terminal_reaction_sync_pending": True,
            "terminal_summary_sync_pending": True,
            "terminal_completion_message_pending": True,
        },
    )

    adapter = DiscordStatusSyncAdapter()
    runner = _make_discord_runner(adapter)

    asyncio.run(_run_one_discord_typing_tick(monkeypatch, runner))

    worker = kb.read_board_metadata(board.slug)["discord_worker"]
    assert "terminal_completion_message_pending" not in worker


def test_discord_kanban_typing_watcher_sends_completion_notice(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from hermes_cli import discord_worker_boards as dwb

    board = dwb.set_goal(
        thread_id="99020",
        goal="Announce completed goal",
        chat_id="parent-99020",
    )
    conn = kb.connect(board=board.slug)
    try:
        task = kb.list_tasks(conn, include_archived=False)[0]
        claimed = kb.claim_task(conn, task.id)
        assert claimed is not None
        kb.complete_task(conn, task.id, summary="done", expected_run_id=claimed.current_run_id)
    finally:
        conn.close()
    dwb._update_worker_meta(
        board.slug,
        {
            "goal_status": "done",
            "phase": "complete",
            "terminal_reaction_sync_pending": True,
            "terminal_summary_sync_pending": True,
            "terminal_completion_message_pending": True,
        },
    )

    adapter = CompletionNoticeAdapter()
    runner = _make_discord_runner(adapter)

    asyncio.run(_run_one_discord_typing_tick(monkeypatch, runner))

    assert len(adapter.completions) == 1
    assert adapter.completions[0]["board"] == board.slug
    worker = kb.read_board_metadata(board.slug)["discord_worker"]
    assert "terminal_completion_message_pending" not in worker


def test_discord_thread_status_targets_mark_foreman_generated_completion(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from hermes_cli import discord_worker_boards as dwb

    board = dwb.start_direct_goal(
        thread_id="99021",
        goal="Foreman escalation: resolve a Discord worker issue.",
        chat_id="parent-99021",
        board_slug="foreman-success-target",
    )
    dwb._update_worker_meta(
        board.slug,
        {
            "goal_status": "done",
            "phase": "complete",
            "terminal_reaction_sync_pending": True,
            "terminal_summary_sync_pending": True,
            "terminal_completion_message_pending": True,
        },
    )

    target = dwb.thread_status_targets()[0]

    assert target["board"] == board.slug
    assert target["state"] == "done"
    assert target["foreman_generated"] is True
    assert target["hide_source_links"] is True
    assert target["terminal_completion_message_pending"] is True


def test_discord_kanban_typing_watcher_skips_blocked_status_sync(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from hermes_cli import discord_worker_boards as dwb

    board = dwb.set_goal(
        thread_id="99008",
        goal="Do not touch blocked Discord thread",
        chat_id="parent-997",
    )
    conn = kb.connect(board=board.slug)
    try:
        task = kb.list_tasks(conn, include_archived=False)[0]
        assert kb.block_task(conn, task.id, reason="waiting for input") is True
    finally:
        conn.close()

    adapter = DiscordStatusSyncAdapter()
    runner = _make_discord_runner(adapter)

    asyncio.run(_run_one_discord_typing_tick(monkeypatch, runner))

    assert adapter.synced == []


def test_discord_kanban_typing_watcher_skips_errored_status_sync(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from hermes_cli import discord_worker_boards as dwb

    board = dwb.set_goal(
        thread_id="99009",
        goal="Do not touch errored Discord thread",
        chat_id="parent-998",
    )
    conn = kb.connect(board=board.slug)
    try:
        task = kb.list_tasks(conn, include_archived=False)[0]
        claimed = kb.claim_task(conn, task.id)
        assert claimed is not None
        blocked = kb._record_spawn_failure(
            conn,
            task.id,
            "worker failed before start",
            failure_limit=1,
        )
        assert blocked is True
    finally:
        conn.close()

    adapter = DiscordStatusSyncAdapter()
    runner = _make_discord_runner(adapter)

    asyncio.run(_run_one_discord_typing_tick(monkeypatch, runner))

    assert adapter.synced == []


def test_discord_worker_status_sync_is_inference_free(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from agent import auxiliary_client
    from hermes_cli import discord_worker_boards as dwb

    call_llm = Mock(side_effect=AssertionError("status sync must not call LLM"))
    async_call_llm = AsyncMock(side_effect=AssertionError("status sync must not call async LLM"))
    monkeypatch.setattr(auxiliary_client, "call_llm", call_llm)
    monkeypatch.setattr(auxiliary_client, "async_call_llm", async_call_llm)

    board = dwb.set_goal(
        thread_id="99010",
        goal="Check active board without inference",
        chat_id="parent-999",
    )

    targets = dwb.thread_status_targets()
    assert [target["board"] for target in targets] == [board.slug]

    adapter = DiscordStatusSyncAdapter()
    runner = _make_discord_runner(adapter)

    asyncio.run(_run_one_discord_typing_tick(monkeypatch, runner))

    assert len(adapter.synced) == 2
    call_llm.assert_not_called()
    async_call_llm.assert_not_called()


def test_discord_kanban_typing_watcher_resyncs_stale_reaction_state(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    monkeypatch.setattr("gateway.run.time.monotonic", lambda: 100.0)
    from hermes_cli import discord_worker_boards as dwb

    board = dwb.set_goal(
        thread_id="99004",
        goal="Repair stale Discord reaction",
        chat_id="parent-993",
    )

    adapter = ReactionSyncAdapter()
    runner = _make_discord_runner(adapter)
    runner._discord_kanban_reaction_states = {
        board.slug: {"state": "active", "synced_at": 10.0},
    }

    asyncio.run(_run_one_discord_typing_tick(monkeypatch, runner))

    assert len(adapter.synced) == 1
    assert adapter.synced[0]["board"] == board.slug
    assert adapter.synced[0]["thread_id"] == "99004"
    cache_key = runner._discord_kanban_target_cache_key(adapter.synced[0])
    assert runner._discord_kanban_reaction_states[cache_key] == {
        "state": "active",
        "synced_at": 100.0,
    }
    assert board.slug not in runner._discord_kanban_reaction_states


def test_discord_kanban_typing_watcher_upgrades_state_only_reaction_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    monkeypatch.setattr("gateway.run.time.monotonic", lambda: 200.0)
    from hermes_cli import discord_worker_boards as dwb

    board = dwb.set_goal(
        thread_id="99006",
        goal="Repair same-state Discord reaction drift",
        chat_id="parent-995",
    )

    adapter = ReactionSyncAdapter()
    runner = _make_discord_runner(adapter)
    runner._discord_kanban_reaction_states = {board.slug: "active"}

    asyncio.run(_run_one_discord_typing_tick(monkeypatch, runner))

    assert len(adapter.synced) == 1
    cache_key = runner._discord_kanban_target_cache_key(adapter.synced[0])
    assert runner._discord_kanban_reaction_states[cache_key] == {
        "state": "active",
        "synced_at": 200.0,
    }
    assert board.slug not in runner._discord_kanban_reaction_states


def test_discord_kanban_typing_watcher_keeps_recent_reaction_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    monkeypatch.setattr("gateway.run.time.monotonic", lambda: 100.0)
    from hermes_cli import discord_worker_boards as dwb

    board = dwb.set_goal(
        thread_id="99005",
        goal="Avoid excessive Discord reaction sync",
        chat_id="parent-994",
    )

    adapter = ReactionSyncAdapter()
    runner = _make_discord_runner(adapter)
    runner._discord_kanban_reaction_states = {
        board.slug: {"state": "active", "synced_at": 90.0},
    }

    asyncio.run(_run_one_discord_typing_tick(monkeypatch, runner))

    assert adapter.synced == []
    cache_key = runner._discord_kanban_target_cache_key(
        {
            "board": board.slug,
            "thread_id": "99005",
            "chat_id": "parent-994",
            "state": "active",
        }
    )
    assert runner._discord_kanban_reaction_states[cache_key] == {
        "state": "active",
        "synced_at": 90.0,
    }
    assert board.slug not in runner._discord_kanban_reaction_states


def test_kanban_notifier_claim_prevents_second_watcher_send(tmp_path, monkeypatch):
    db_path = tmp_path / "single-owner.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    tid = _create_completed_subscription()

    adapter1 = RecordingAdapter()
    adapter2 = RecordingAdapter()

    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter1)))
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter2)))

    assert len(adapter1.sent) == 1
    assert adapter2.sent == []


def test_kanban_notifier_rewinds_claim_if_adapter_disconnects(tmp_path, monkeypatch):
    db_path = tmp_path / "adapter-disconnect.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    tid = _create_completed_subscription()

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = DisconnectedAdapters({Platform.TELEGRAM: RecordingAdapter()})
    runner._kanban_sub_fail_counts = {}

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert [ev.kind for ev in _unseen_terminal_events(tid)] == ["completed"]


def test_kanban_db_path_is_test_isolated_from_real_home():
    hermes_home = Path(kb.kanban_home())
    production_db = Path.home() / ".hermes" / "kanban.db"
    assert kb.kanban_db_path().resolve() != production_db.resolve()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
    finally:
        conn.close()

    assert kb.kanban_db_path().resolve().is_relative_to(hermes_home.resolve())
    assert kb.kanban_db_path().resolve() != production_db.resolve()


class FailingAdapter:
    """Adapter whose send() always raises, simulating a transient send error."""

    def __init__(self):
        self.attempts = 0

    async def send(self, chat_id, text, metadata=None):
        self.attempts += 1
        raise RuntimeError("simulated send failure")


def test_kanban_notifier_rewinds_claim_on_send_exception(tmp_path, monkeypatch):
    """A raising adapter rewinds the claim so the next tick can retry.

    This is the second rewind path (distinct from the adapter-disconnect path
    in test_kanban_notifier_rewinds_claim_if_adapter_disconnects). Here the
    adapter is connected and the send call actually fires; the claim must
    still rewind so the event isn't lost when send() raises mid-tick.
    """
    db_path = tmp_path / "send-failure.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    tid = _create_completed_subscription()

    adapter = FailingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # Send was attempted (so we exercised the failure path, not just the
    # disconnect path) and the claim was rewound — the unseen-events query
    # still returns the event for retry on the next tick.
    assert adapter.attempts >= 1, "send should have been attempted at least once"
    assert [ev.kind for ev in _unseen_terminal_events(tid)] == ["completed"]


def test_notifier_redelivers_same_kind_on_dispatch_cycle(tmp_path, monkeypatch):
    """A retry cycle (crashed → reclaimed → crashed) notifies the user twice.

    Before #21398 the notifier auto-unsubscribed on any terminal event kind
    (gave_up / crashed / timed_out), so the second crash in a respawn cycle
    silently dropped — the subscription was already gone. This test pins the
    new contract: subscription survives non-final terminal events; the
    cursor handles dedup.

    Two crashes ten seconds apart on the same task — both should land on
    the adapter.
    """
    db_path = tmp_path / "redeliver-cycle.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="cycle test", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        # First crash — fired by the dispatcher when the worker PID dies.
        kb._append_event(conn, tid, kind="crashed")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # First crash delivered.
    assert len(adapter.sent) == 1
    assert "crashed" in adapter.sent[0]["text"].lower()

    # Subscription survives — the cursor advanced past event #1, but the
    # row is still there.
    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, tid)
        assert len(subs) == 1, (
            "Subscription must survive a crashed event so a respawn-cycle "
            "second crash also notifies the user (issue #21398)."
        )

        # Second crash — same task, same dispatcher (or a respawn). Append
        # another event to simulate the dispatcher firing crashed a second
        # time during retry.
        kb._append_event(conn, tid, kind="crashed")
    finally:
        conn.close()

    # New tick: the second event has a fresh id past the cursor advance,
    # so it gets claimed and delivered.
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 2, (
        f"Second crashed event should also notify; got {len(adapter.sent)} "
        f"deliveries (texts: {[d['text'] for d in adapter.sent]})"
    )
    assert "crashed" in adapter.sent[1]["text"].lower()
