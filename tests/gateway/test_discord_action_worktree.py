from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource
from tools.canonical_repo_guard import canonical_main_terminal_violation


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True)
    _run(repo, "init")
    _run(repo, "config", "user.email", "tests@example.invalid")
    _run(repo, "config", "user.name", "Hermes Tests")
    _run(repo, "checkout", "-b", "main")
    (repo / "tracked.txt").write_text("original\n", encoding="utf-8")
    _run(repo, "add", "tracked.txt")
    _run(repo, "commit", "-m", "initial")


def _protect(monkeypatch, root: Path) -> None:
    monkeypatch.setenv("HERMES_CANONICAL_REPO_ROOTS", str(root))
    monkeypatch.delenv("HERMES_DISABLE_CANONICAL_REPO_GUARD", raising=False)
    monkeypatch.delenv("HERMES_ALLOW_CANONICAL_MAIN_WRITES", raising=False)


def _source(project_path: Path, *, chat_type: str = "thread") -> SessionSource:
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id="thread-123",
        chat_name="Sligo Labs / #pid / no-op",
        chat_type=chat_type,
        user_id="user-1",
        guild_id="guild-1",
        parent_chat_id="project-channel-1",
        thread_id="thread-123",
        project_name="PID",
        project_path=str(project_path),
        project_channel_id="project-channel-1",
        project_mapping_source="configured_channel_cwd",
        project_mapping_resolved=True,
    )


def _feature_summary() -> dict[str, str]:
    return {"initial_request": "do a no-op change end-to-end"}


def _config(default_cwd: Path) -> dict:
    return {"terminal": {"cwd": str(default_cwd)}}


def test_normal_action_creates_and_reuses_thread_worktree(tmp_path, monkeypatch):
    canonical_root = tmp_path / "canonical"
    canonical = canonical_root / "PID"
    workspaces = tmp_path / "workspaces"
    _init_repo(canonical)
    _protect(monkeypatch, canonical_root)
    monkeypatch.setattr(gateway_run, "_DISCORD_ACTION_WORKTREE_ROOT", workspaces)

    source = _source(canonical)
    session_key = "agent:main:discord:thread:thread-123"
    cwd, error, worktree_cwd = gateway_run._resolve_gateway_turn_cwd(
        source,
        _feature_summary(),
        _config(canonical),
        session_key,
    )

    assert error is None
    assert worktree_cwd == cwd
    worktree = Path(cwd)
    assert worktree.is_dir()
    assert worktree.parent == workspaces
    assert _run(worktree, "branch", "--show-current").stdout.strip().startswith(
        "discord-action/pid-"
    )
    assert canonical_main_terminal_violation(canonical, "touch blocked.txt") is not None
    assert canonical_main_terminal_violation(worktree, "touch allowed.txt") is None

    reused_cwd, reused_error, reused_worktree = gateway_run._resolve_gateway_turn_cwd(
        source,
        _feature_summary(),
        _config(canonical),
        session_key,
    )

    assert reused_error is None
    assert reused_cwd == cwd
    assert reused_worktree == cwd


def test_action_worktree_branch_and_path_advance_together_on_collision(
    tmp_path,
    monkeypatch,
):
    canonical_root = tmp_path / "canonical"
    canonical = canonical_root / "PID"
    workspaces = tmp_path / "workspaces"
    _init_repo(canonical)
    _protect(monkeypatch, canonical_root)
    monkeypatch.setattr(gateway_run, "_DISCORD_ACTION_WORKTREE_ROOT", workspaces)

    source = _source(canonical)
    session_key = "agent:main:discord:thread:thread-123"
    base_branch, base_path = gateway_run._discord_action_worktree_target(
        canonical.resolve(),
        source,
        session_key,
    )
    occupied = tmp_path / "occupied" / "other-worktree"
    occupied.parent.mkdir()
    _run(canonical, "worktree", "add", "-b", base_branch, str(occupied), "HEAD")

    cwd, error, worktree_cwd = gateway_run._resolve_gateway_turn_cwd(
        source,
        _feature_summary(),
        _config(canonical),
        session_key,
    )

    assert error is None
    assert worktree_cwd == cwd
    assert Path(cwd) == Path(f"{base_path}-2")
    assert _run(Path(cwd), "branch", "--show-current").stdout.strip() == f"{base_branch}-2"


def test_action_worktree_failure_is_explicit_and_keeps_canonical_guarded(
    tmp_path,
    monkeypatch,
):
    canonical_root = tmp_path / "canonical"
    canonical = canonical_root / "PID"
    unavailable_root = tmp_path / "not-a-directory"
    _init_repo(canonical)
    unavailable_root.write_text("occupied\n", encoding="utf-8")
    _protect(monkeypatch, canonical_root)
    monkeypatch.setattr(
        gateway_run,
        "_DISCORD_ACTION_WORKTREE_ROOT",
        unavailable_root,
    )

    cwd, error, worktree_cwd = gateway_run._resolve_gateway_turn_cwd(
        _source(canonical),
        _feature_summary(),
        _config(canonical),
        "agent:main:discord:thread:thread-123",
    )

    assert cwd == str(canonical)
    assert worktree_cwd is None
    assert error is not None
    assert "could not prepare the mutable action-worktree root" in error
    assert canonical_main_terminal_violation(canonical, "touch still-blocked.txt") is not None


def test_non_action_gateway_route_keeps_existing_cwd(tmp_path, monkeypatch):
    canonical_root = tmp_path / "canonical"
    canonical = canonical_root / "PID"
    _init_repo(canonical)
    _protect(monkeypatch, canonical_root)
    monkeypatch.setattr(
        gateway_run,
        "_DISCORD_ACTION_WORKTREE_ROOT",
        tmp_path / "workspaces",
    )

    cwd, error, worktree_cwd = gateway_run._resolve_gateway_turn_cwd(
        _source(canonical, chat_type="group"),
        _feature_summary(),
        _config(canonical),
        "agent:main:discord:group:project-channel-1",
    )

    assert cwd == str(canonical)
    assert error is None
    assert worktree_cwd is None
    assert not (tmp_path / "workspaces").exists()


def _runner_for_action_turn(tmp_path, captured: dict) -> gateway_run.GatewayRunner:
    runner = gateway_run.GatewayRunner(GatewayConfig())
    runner.adapters = {}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = None
    runner._recover_telegram_topic_thread_id = lambda _source: None
    runner._is_session_run_current = lambda _key, _generation: True
    runner._reply_anchor_for_event = lambda _event: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    runner._discord_work_item_id_for_event = lambda *_args, **_kwargs: None
    runner._register_discord_summary_post_delivery = lambda **_kwargs: None
    runner._cache_session_source = lambda key, source: captured.update(
        cached_key=key,
        cached_source=source,
    )
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()

    def _set_session_env(context, *, session_cwd=""):
        captured["context_source"] = context.source
        captured["session_env_cwd"] = session_cwd
        return []

    runner._set_session_env = _set_session_env
    runner._clear_session_env = lambda _tokens: None
    runner.session_store = MagicMock()
    now = datetime.now()
    runner.session_store.get_or_create_session.return_value = SessionEntry(
        session_key="agent:main:discord:thread:thread-123",
        session_id="session-1",
        created_at=now,
        updated_at=now,
        platform=Platform.DISCORD,
        chat_type="thread",
    )
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_platform_message_id.return_value = False
    runner.session_store.update_session = MagicMock()
    runner.session_store.append_to_transcript = MagicMock()
    runner._run_agent = AsyncMock(
        return_value={
            "final_response": "done",
            "messages": [
                {"role": "user", "content": "do a no-op change end-to-end"},
                {"role": "assistant", "content": "done"},
            ],
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 0,
        }
    )
    return runner


@pytest.mark.asyncio
@pytest.mark.parametrize("fable_implementation", [False, True])
async def test_action_turn_injects_worktree_as_project_path_and_agent_cwd(
    tmp_path,
    monkeypatch,
    fable_implementation,
):
    canonical_root = tmp_path / "canonical"
    canonical = canonical_root / "PID"
    workspaces = tmp_path / "workspaces"
    _init_repo(canonical)
    _protect(monkeypatch, canonical_root)
    monkeypatch.setattr(gateway_run, "_DISCORD_ACTION_WORKTREE_ROOT", workspaces)
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda: _config(canonical),
    )
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {"api_key": "fake"},
    )
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *_args, **_kwargs: 100_000,
    )

    captured: dict = {}
    runner = _runner_for_action_turn(tmp_path, captured)
    source = _source(canonical)
    event = MessageEvent(
        text="do a no-op change end-to-end",
        source=source,
        message_id="message-1",
    )
    event.feature_summary = _feature_summary()
    if fable_implementation:
        event.fable_plan_metadata = {
            "command": "fable",
            "fable_mode": "implementation",
        }
        runner._run_agent.return_value.update(
            model="claude-fable-5",
            provider="anthropic",
        )

    response = await runner._handle_message_with_agent(
        event,
        source,
        "agent:main:discord:thread:thread-123",
        1,
    )

    assert response == "done"
    run_kwargs = runner._run_agent.await_args.kwargs
    worktree_cwd = run_kwargs["session_cwd_override"]
    assert Path(worktree_cwd).parent == workspaces
    assert run_kwargs["source"].project_path == worktree_cwd
    assert event.source.project_path == worktree_cwd
    assert captured["context_source"].project_path == worktree_cwd
    assert captured["session_env_cwd"] == worktree_cwd
    assert str(canonical) not in run_kwargs["context_prompt"]
    assert f"Path: `{worktree_cwd}`" in run_kwargs["context_prompt"]
