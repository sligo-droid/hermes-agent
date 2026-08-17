from __future__ import annotations

import logging
import os
import subprocess
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from agent.verification_evidence import record_terminal_result
from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource
from gateway.work_ledger import GatewayWorkLedger
from hermes_cli.config import DEFAULT_CONFIG
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


def _commit_lockfile(repo: Path, name: str) -> None:
    (repo / name).write_text("lockfile\n", encoding="utf-8")
    _run(repo, "add", name)
    _run(repo, "commit", "-m", f"add {name}")


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


def _config(default_cwd: Path, **discord: object) -> dict:
    config: dict = {"terminal": {"cwd": str(default_cwd)}}
    if discord:
        config["discord"] = discord
    return config


def test_closeout_raw_config_normalizes_non_mapping_sections():
    assert gateway_run._closeout_mapping(["ci", "restart"]) == {}
    assert gateway_run._closeout_repository_config(
        {"repositories": {"owner/repo": "gateway-self"}},
        "owner/repo",
    ) == {}


def test_closeout_repository_overrides_are_generic_and_nested():
    effective = gateway_run._effective_closeout_repository_config(
        {
            "mode": "shadow",
            "surfaces": {"direct": False, "kanban": True},
            "early_draft_pr": False,
            "preview": {"required": False},
            "repositories": {
                "acme/pid": {
                    "mode": "enforce",
                    "surfaces": {"direct": True},
                    "early_draft_pr": True,
                    "visual_qa": {"mode": "enforce_explicit"},
                    "preview": {"required": True},
                }
            },
        },
        "acme/pid",
    )

    assert effective["mode"] == "enforce"
    assert effective["surfaces"] == {"direct": True, "kanban": True}
    assert effective["early_draft_pr"] is True
    assert effective["visual_qa"] == {"mode": "enforce_explicit"}
    assert effective["preview"] == {"required": True}


def test_action_workspace_origin_overrides_stale_discord_mapping(tmp_path, caplog):
    repo = tmp_path / "PID"
    _init_repo(repo)
    _run(repo, "remote", "add", "origin", "https://github.com/sligo-labs/PID.git")
    source = _source(repo)
    source.project_github_url = "https://github.com/sligo-droid/PID"

    with caplog.at_level(logging.WARNING):
        repository = gateway_run._gateway_repository_for_source(
            source,
            workspace_path=str(repo),
        )

    assert repository == "sligo-labs/PID"
    assert "repository mapping mismatch" in caplog.text
    mode, _policy = gateway_run._gateway_action_closeout_contract(
        {
            "closeout": {
                "mode": "shadow",
                "surfaces": {"direct": True},
                "repositories": {
                    "sligo-labs/PID": {"mode": "enforce"},
                },
            }
        },
        repository=repository,
        request="implement and ship this end-to-end",
        source="direct",
    )
    assert mode == "enforce"


def test_discord_acceptance_applies_repository_visual_override(tmp_path, monkeypatch):
    config = {
        "agent": {"visual_qa": {"mode": "shadow", "max_followup_turns": 1}},
        "closeout": {
            "repositories": {
                "acme/pid": {"visual_qa": {"mode": "enforce_explicit"}}
            }
        },
    }
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: config)
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.work_ledger = GatewayWorkLedger(tmp_path / "ledger.json")
    source = _source(tmp_path)
    source.project_github_url = "https://github.com/acme/pid"
    event = MessageEvent(
        text="Build a responsive dashboard with a mobile sidebar.",
        source=source,
        message_id="repo-visual-override",
        discord_runtime_mode="action",
    )

    item = runner._accept_discord_work_item(
        event,
        "agent:main:discord:thread:thread-123",
    )

    assert item is not None
    assert item["visual_qa_config"]["mode"] == "enforce_explicit"
    assert event.visual_qa_config["mode"] == "enforce_explicit"
    assert event.visual_qa_requirement["level"] == "surface"
    assert gateway_run._closeout_repository_config(
        {"repositories": ["owner/repo"]},
        "owner/repo",
    ) == {}


def test_discord_reacceptance_promotes_stale_shadow_visual_config(tmp_path, monkeypatch):
    config = {"agent": {"visual_qa": {"mode": "shadow"}}}
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: config)
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.work_ledger = GatewayWorkLedger(tmp_path / "ledger.json")
    source = _source(tmp_path)
    source.project_github_url = "https://github.com/acme/pid"
    event = MessageEvent(
        text="Build a responsive dashboard with a mobile sidebar.",
        source=source,
        message_id="repo-visual-promotion",
        discord_runtime_mode="action",
    )
    session_key = "agent:main:discord:thread:thread-123"

    first = runner._accept_discord_work_item(event, session_key)
    assert first["visual_qa_config"]["mode"] == "shadow"
    config["closeout"] = {
        "repositories": {
            "acme/pid": {"visual_qa": {"mode": "enforce_explicit"}}
        }
    }
    replay = MessageEvent(
        text=event.text,
        source=source,
        message_id=event.message_id,
        discord_runtime_mode="action",
    )
    second = runner._accept_discord_work_item(replay, session_key)

    assert second["visual_qa_config"]["mode"] == "enforce_explicit"
    assert replay.visual_qa_config["mode"] == "enforce_explicit"


def test_action_worktree_warmup_config_defaults():
    discord = DEFAULT_CONFIG["discord"]

    assert discord["action_worktree_warmup"] == "auto"
    assert discord["action_worktree_warmup_timeout_seconds"] == 180


@pytest.mark.parametrize(
    ("lockfile", "expected_command", "expected_log"),
    [
        (
            "pnpm-lock.yaml",
            ["pnpm", "install", "--frozen-lockfile", "--prefer-offline"],
            "warmup pnpm",
        ),
        (
            "package-lock.json",
            ["npm", "ci", "--prefer-offline", "--no-audit", "--no-fund"],
            "warmup npm",
        ),
        ("yarn.lock", None, "warmup skipped: yarn.lock has ambiguous yarn variants"),
        (None, None, "warmup skipped: no lockfile"),
    ],
)
def test_action_worktree_warmup_lockfile_detection_matrix(
    tmp_path,
    monkeypatch,
    caplog,
    lockfile,
    expected_command,
    expected_log,
):
    if lockfile:
        (tmp_path / lockfile).write_text("lockfile\n", encoding="utf-8")
    calls = []

    def _fake_run(command, *, cwd, timeout):
        calls.append((command, cwd, timeout))
        return subprocess.CompletedProcess(command, 0, stdout="ready")

    monkeypatch.setattr(
        gateway_run,
        "_run_discord_action_worktree_warmup",
        _fake_run,
    )

    with caplog.at_level(logging.INFO, logger="gateway.run"):
        gateway_run._discord_action_worktree_warmup(tmp_path, {})

    if expected_command is None:
        assert calls == []
    else:
        assert len(calls) == 1
        assert calls[0][0] == expected_command
        assert calls[0][1] == tmp_path
        assert calls[0][2] == pytest.approx(180.0, abs=0.02)
        assert " ok" in caplog.text
    assert expected_log in caplog.text


def test_action_worktree_warmup_off_gate_skips_install(tmp_path, monkeypatch, caplog):
    canonical_root = tmp_path / "canonical"
    canonical = canonical_root / "PID"
    workspaces = tmp_path / "workspaces"
    _init_repo(canonical)
    _commit_lockfile(canonical, "pnpm-lock.yaml")
    _protect(monkeypatch, canonical_root)
    monkeypatch.setattr(gateway_run, "_DISCORD_ACTION_WORKTREE_ROOT", workspaces)

    def _unexpected_run(*_args, **_kwargs):
        pytest.fail("warm-up subprocess should be disabled")

    monkeypatch.setattr(
        gateway_run,
        "_run_discord_action_worktree_warmup",
        _unexpected_run,
    )

    with caplog.at_level(logging.INFO, logger="gateway.run"):
        cwd, error, worktree_cwd = gateway_run._resolve_gateway_turn_cwd(
            _source(canonical),
            _feature_summary(),
            _config(canonical, action_worktree_warmup="off"),
            "agent:main:discord:thread:thread-123",
        )

    assert error is None
    assert worktree_cwd == cwd
    assert Path(cwd).is_dir()
    assert "warmup skipped: off" in caplog.text


def test_action_worktree_warmup_timeout_does_not_fail_provisioning(
    tmp_path,
    monkeypatch,
    caplog,
):
    canonical_root = tmp_path / "canonical"
    canonical = canonical_root / "PID"
    workspaces = tmp_path / "workspaces"
    _init_repo(canonical)
    _commit_lockfile(canonical, "pnpm-lock.yaml")
    _protect(monkeypatch, canonical_root)
    monkeypatch.setattr(gateway_run, "_DISCORD_ACTION_WORKTREE_ROOT", workspaces)

    def _timeout(command, *, cwd, timeout):
        assert cwd.parent == workspaces
        assert timeout == pytest.approx(0.25, abs=0.02)
        raise subprocess.TimeoutExpired(command, timeout, output="still installing")

    monkeypatch.setattr(
        gateway_run,
        "_run_discord_action_worktree_warmup",
        _timeout,
    )

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        cwd, error, worktree_cwd = gateway_run._resolve_gateway_turn_cwd(
            _source(canonical),
            _feature_summary(),
            _config(canonical, action_worktree_warmup_timeout_seconds=0.25),
            "agent:main:discord:thread:thread-123",
        )

    assert error is None
    assert worktree_cwd == cwd
    assert Path(cwd).is_dir()
    assert "warmup failed (continuing): pnpm timed out" in caplog.text
    assert "still installing" in caplog.text


def test_action_worktree_warmup_failure_does_not_fail_provisioning(
    tmp_path,
    monkeypatch,
    caplog,
):
    canonical_root = tmp_path / "canonical"
    canonical = canonical_root / "PID"
    workspaces = tmp_path / "workspaces"
    _init_repo(canonical)
    _commit_lockfile(canonical, "package-lock.json")
    _protect(monkeypatch, canonical_root)
    monkeypatch.setattr(gateway_run, "_DISCORD_ACTION_WORKTREE_ROOT", workspaces)

    noisy_output = "install failed sk-exampletoken1234567890 " + ("x" * 2_000)

    def _failure(command, *, cwd, timeout):
        assert cwd.parent == workspaces
        assert timeout == pytest.approx(180.0, abs=0.02)
        return subprocess.CompletedProcess(command, 17, stdout=noisy_output)

    monkeypatch.setattr(
        gateway_run,
        "_run_discord_action_worktree_warmup",
        _failure,
    )

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        cwd, error, worktree_cwd = gateway_run._resolve_gateway_turn_cwd(
            _source(canonical),
            _feature_summary(),
            _config(canonical),
            "agent:main:discord:thread:thread-123",
        )

    assert error is None
    assert worktree_cwd == cwd
    assert Path(cwd).is_dir()
    assert "warmup failed (continuing): npm exited 17" in caplog.text
    assert "install failed" in caplog.text
    assert "sk-exampletoken1234567890" not in caplog.text
    assert "x" * 801 not in caplog.text


def test_action_worktree_warmup_detects_nested_pnpm_package(tmp_path, monkeypatch):
    package = tmp_path / "dashboard"
    package.mkdir()
    (package / "package.json").write_text('{"name":"dashboard"}', encoding="utf-8")
    (package / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
    calls = []

    monkeypatch.setattr(
        gateway_run,
        "_run_discord_action_worktree_warmup",
        lambda command, *, cwd, timeout: (
            calls.append((command, cwd, timeout))
            or subprocess.CompletedProcess(command, 0, stdout="ready")
        ),
    )

    gateway_run._discord_action_worktree_warmup(tmp_path, {})

    assert calls[0][0] == ["pnpm", "install", "--frozen-lockfile", "--prefer-offline"]
    assert calls[0][1] == package


def test_action_worktree_warmup_skips_prepared_dependency_link(tmp_path, monkeypatch, caplog):
    (tmp_path / "package.json").write_text('{"name":"root"}', encoding="utf-8")
    (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
    shared = tmp_path.parent / "shared-node-modules"
    shared.mkdir()
    (tmp_path / "node_modules").symlink_to(shared, target_is_directory=True)
    monkeypatch.setattr(
        gateway_run,
        "_run_discord_action_worktree_warmup",
        lambda *_args, **_kwargs: pytest.fail("linked dependencies must skip install"),
    )

    with caplog.at_level(logging.INFO, logger="gateway.run"):
        gateway_run._discord_action_worktree_warmup(tmp_path, {})

    assert "exact-lock node_modules link already prepared" in caplog.text


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


def test_restarted_action_turn_restores_closeout_contract(tmp_path, monkeypatch):
    canonical_root = tmp_path / "canonical"
    canonical = canonical_root / "PID"
    workspaces = tmp_path / "workspaces"
    _init_repo(canonical)
    _protect(monkeypatch, canonical_root)
    monkeypatch.setattr(gateway_run, "_DISCORD_ACTION_WORKTREE_ROOT", workspaces)
    session_key = "agent:main:discord:thread:thread-123"
    feature_summary = {
        "initial_request": "please resume",
        "project_context": {"project_path": str(canonical)},
    }
    cwd, error, _ = gateway_run._resolve_gateway_turn_cwd(
        _source(canonical), feature_summary, _config(canonical), session_key
    )
    assert error is None

    recovered_source = _source(Path(cwd))
    resumed_cwd, resumed_error, action_worktree = gateway_run._resolve_gateway_turn_cwd(
        recovered_source, feature_summary, _config(canonical), session_key
    )
    assert resumed_error is None
    assert resumed_cwd == action_worktree == cwd

    runner = object.__new__(gateway_run.GatewayRunner)
    runner.work_ledger = GatewayWorkLedger(tmp_path / "ledger.json")
    event = MessageEvent(
        text="please resume",
        source=recovered_source,
        message_id="resume-message",
        discord_runtime_mode="action",
        feature_summary=feature_summary,
    )
    item = runner.work_ledger.accept_event(
        event, session_key=session_key, freshness_seconds=60
    )
    event.work_item_id = item["id"]
    state = runner._persist_action_closeout_workspace(
        event,
        mutable_path=action_worktree,
        canonical_path=recovered_source.project_path,
        config={},
        mode="enforce",
        policy={"merge": "never"},
    )
    assert state["workspace"]["canonical_path"] == str(canonical)

    runner.work_ledger.mark_agent_done(
        item["id"],
        final_response="PR ready. It is not merged; approval is pending.",
    )
    assert runner.work_ledger.get(item["id"])["completion_gate"]["allowed_to_complete"] is True


def test_read_only_turn_never_provisions_but_can_reuse_existing_action_worktree(
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

    read_cwd, read_error, read_worktree = gateway_run._resolve_gateway_turn_cwd(
        source,
        _feature_summary(),
        _config(canonical),
        session_key,
        "read_only",
    )
    assert read_cwd == str(canonical)
    assert read_error is None
    assert read_worktree is None
    assert not workspaces.exists()

    action_cwd, action_error, action_worktree = gateway_run._resolve_gateway_turn_cwd(
        source,
        _feature_summary(),
        _config(canonical),
        session_key,
        "action",
    )
    assert action_error is None
    assert action_worktree == action_cwd

    reused_cwd, reused_error, reused_worktree = gateway_run._resolve_gateway_turn_cwd(
        source,
        _feature_summary(),
        _config(canonical),
        session_key,
        "read_only",
    )
    assert reused_error is None
    assert reused_cwd == action_cwd
    assert reused_worktree == action_cwd


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


def test_next_pr_generation_uses_distinct_worktree_from_refreshed_remote_main(
    tmp_path,
    monkeypatch,
):
    canonical_root = tmp_path / "canonical"
    canonical = canonical_root / "PID"
    remote = tmp_path / "pid-origin.git"
    updater = tmp_path / "updater"
    workspaces = tmp_path / "workspaces"
    _init_repo(canonical)
    _run(tmp_path, "init", "--bare", str(remote))
    _run(canonical, "remote", "add", "origin", str(remote))
    _run(canonical, "push", "-u", "origin", "main")
    _protect(monkeypatch, canonical_root)
    monkeypatch.setattr(gateway_run, "_DISCORD_ACTION_WORKTREE_ROOT", workspaces)
    source = _source(canonical)
    session_key = "agent:main:discord:thread:thread-123"

    first_cwd, first_error, _first_worktree = gateway_run._resolve_gateway_turn_cwd(
        source,
        _feature_summary(),
        _config(canonical),
        session_key,
        "action",
        1,
    )
    assert first_error is None

    _run(tmp_path, "clone", str(remote), str(updater))
    _run(updater, "config", "user.email", "tests@example.invalid")
    _run(updater, "config", "user.name", "Hermes Tests")
    _run(updater, "checkout", "main")
    (updater / "merged-pr-1.txt").write_text("merged\n", encoding="utf-8")
    _run(updater, "add", "merged-pr-1.txt")
    _run(updater, "commit", "-m", "merge first PR")
    _run(updater, "push", "origin", "main")
    remote_main = _run(updater, "rev-parse", "HEAD").stdout.strip()
    source = _source(Path(first_cwd))

    second_cwd, second_error, _second_worktree = gateway_run._resolve_gateway_turn_cwd(
        source,
        _feature_summary(),
        _config(canonical),
        session_key,
        "action",
        2,
    )

    assert second_error is None
    assert second_cwd != first_cwd
    assert Path(second_cwd).name.endswith("-pr2")
    assert (Path(second_cwd) / "merged-pr-1.txt").read_text(encoding="utf-8") == "merged\n"
    assert _run(Path(second_cwd), "rev-parse", "HEAD").stdout.strip() == remote_main
    assert _run(Path(second_cwd), "branch", "--show-current").stdout.strip().endswith(
        "-pr2"
    )


def test_rollover_fetch_failure_does_not_reuse_merged_worktree(
    tmp_path,
    monkeypatch,
):
    canonical_root = tmp_path / "canonical"
    canonical = canonical_root / "PID"
    workspaces = tmp_path / "workspaces"
    _init_repo(canonical)
    _run(canonical, "remote", "add", "origin", str(tmp_path / "missing-origin.git"))
    _protect(monkeypatch, canonical_root)
    monkeypatch.setattr(gateway_run, "_DISCORD_ACTION_WORKTREE_ROOT", workspaces)
    source = _source(canonical)

    cwd, error, worktree_cwd = gateway_run._resolve_gateway_turn_cwd(
        source,
        _feature_summary(),
        _config(canonical),
        "agent:main:discord:thread:thread-123",
        "action",
        2,
    )

    assert cwd == str(canonical)
    assert worktree_cwd is None
    assert error is not None
    assert "could not refresh the merged base" in error.lower()
    assert not workspaces.exists() or not any(workspaces.iterdir())


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


def test_resolved_action_workspace_is_persisted_separately_before_activation(tmp_path):
    canonical = tmp_path / "canonical"
    mutable = tmp_path / "mutable"
    _init_repo(canonical)
    _run(canonical, "worktree", "add", "-b", "discord/action-test", str(mutable), "HEAD")
    captured = {}

    class Ledger:
        def get(self, work_id):
            return {"id": work_id, "closeout": {"mode": "off"}}

        def attach_closeout_workspace(self, work_id, **kwargs):
            captured.update(work_id=work_id, **kwargs)
            return {"mode": kwargs["mode"], "workspace": {"path": kwargs["workspace_path"]}}

    runner = object.__new__(gateway_run.GatewayRunner)
    runner.work_ledger = Ledger()
    runner.trusted_closeout_watcher = None
    event = SimpleNamespace(work_item_id="work-1")

    state = runner._persist_action_closeout_workspace(
        event,
        mutable_path=str(mutable),
        canonical_path=str(canonical),
        config={"closeout": {"repositories": {}}},
        source="direct",
        mode="off",
    )

    assert state["mode"] == "off"
    assert captured["workspace_path"] == str(mutable.resolve())
    assert captured["canonical_path"] == str(canonical)
    assert captured["branch"] == "discord/action-test"
    assert captured["base_branch"] == "main"
    assert captured["source"] == "direct"


def _direct_closeout_runner(
    mutable,
    *,
    mode="shadow",
    require_visual_qa=False,
    activated=False,
    source="direct",
):
    captured = {}
    item = {
        "id": "work-1",
        "closeout_authoritative": False,
        "closeout": {
            "revision": 4,
            "source": source,
            "mode": mode,
            "workspace": {
                "path": str(mutable),
                "repository": "acme/example",
                "branch": "discord/action-test",
                "base_branch": "main",
            },
            "policy": {"require_visual_qa": require_visual_qa},
        },
        "visual_qa_requirement": {"id": "trusted-requirement"},
    }
    if activated:
        item["closeout_activated_at"] = 100.0
        item["closeout"]["pr"] = {
            "head_sha": _run(mutable, "rev-parse", "HEAD").stdout.strip(),
            "url": "https://github.com/acme/example/pull/1",
        }
        item["closeout"]["local_verification"] = {
            "status": "passed",
            "head_sha": item["closeout"]["pr"]["head_sha"],
        }
        item["closeout"]["status"] = "waiting_for_ci"

    class Ledger:
        checkpoint = None

        def get(self, work_id):
            return item if work_id == item["id"] else None

        def record_required_async_checkpoint(self, work_id, **kwargs):
            incoming = {
                key: kwargs[key]
                for key in (
                    "parent_sha",
                    "tree_sha",
                    "message",
                    "repository_root",
                    "workspace_path",
                )
            }
            if kwargs.get("committed_head_sha"):
                incoming["committed_head_sha"] = kwargs["committed_head_sha"]
            if self.checkpoint and self.checkpoint.get("committed_head_sha"):
                incoming.setdefault(
                    "committed_head_sha",
                    self.checkpoint["committed_head_sha"],
                )
            self.checkpoint = incoming
            return {"checkpoint": dict(incoming)}

        def activate_closeout(self, work_id, state, *, expected_revision):
            captured.update(
                work_id=work_id,
                state=state,
                expected_revision=expected_revision,
            )
            return {**state, "revision": expected_revision + 1}

        def publish_closeout_verified_head(
            self,
            work_id,
            *,
            expected_head_sha,
            verified_head_sha,
        ):
            captured.update(
                work_id=work_id,
                published_from_head_sha=expected_head_sha,
                published_head_sha=verified_head_sha,
            )
            state = dict(item["closeout"])
            state["local_verification"] = {
                "status": "passed",
                "head_sha": verified_head_sha,
            }
            state["pr"] = {**state["pr"], "head_sha": verified_head_sha}
            state["ci"] = {"status": "not_checked", "head_sha": verified_head_sha}
            state["visual_qa"] = {"status": "pending", "head_sha": verified_head_sha}
            state["revision"] = int(state.get("revision") or 0) + 1
            item["closeout"] = state
            return state

        def apply_closeout_visual_completion(
            self,
            work_id,
            *,
            expected_head_sha,
            receipts,
            min_receipt_order,
        ):
            captured.update(
                work_id=work_id,
                applied_head_sha=expected_head_sha,
                receipts=receipts,
                min_receipt_order=min_receipt_order,
            )
            state = dict(item["closeout"])
            state["visual_qa"] = {
                "status": "passed",
                "head_sha": expected_head_sha,
            }
            state["revision"] = int(state.get("revision") or 0) + 1
            item["closeout"] = state
            return state

    notifications = []
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.work_ledger = Ledger()
    runner.trusted_closeout_watcher = SimpleNamespace(
        notify=lambda work_id: notifications.append(work_id)
    )
    return runner, captured, notifications


def _successful_verification_result(mutable, *, mutation_generation=0, mutation_boundary=0):
    head_sha = _run(mutable, "rev-parse", "HEAD").stdout.strip()
    return {
        "runtime_breakdown": {
            "mutation_generation": mutation_generation,
            "mutation_boundary": mutation_boundary,
            "verification_evidence": [
                {
                    "surface": "verification",
                    "status": "success",
                    "order": 1,
                    "repository_root": str(mutable.resolve()),
                    "canonical_command": "scripts/run_tests.sh",
                    "scope": "full",
                    "mutation_generation": mutation_generation,
                    "mutation_boundary": mutation_boundary,
                    "verified_head_sha": head_sha,
                }
            ],
        }
    }


def test_fable_closeout_compatibility_preserves_legacy_opt_outs():
    base = {
        "closeout": {
            "mode": "enforce",
            "surfaces": {"direct": True},
        }
    }

    ordinary_mode, ordinary_policy = gateway_run._gateway_action_closeout_contract(
        base,
        repository="acme/example",
        request="implement and merge this",
        source="fable",
    )
    none_mode, _ = gateway_run._gateway_action_closeout_contract(
        {**base, "fable": {"git_lifecycle": "none"}},
        repository="acme/example",
        request="implement and merge this",
        source="fable",
    )
    disabled_mode, _ = gateway_run._gateway_action_closeout_contract(
        {
            **base,
            "closeout": {
                **base["closeout"],
                "surfaces": {"direct": True, "fable": False},
            },
        },
        repository="acme/example",
        request="implement and merge this",
        source="fable",
    )
    pr_mode, pr_policy = gateway_run._gateway_action_closeout_contract(
        {**base, "fable": {"git_lifecycle": "pr"}},
        repository="acme/example",
        request="implement and merge this",
        source="fable",
    )

    assert ordinary_mode == "enforce"
    assert ordinary_policy["merge"] == "never"
    assert none_mode == "off"
    assert disabled_mode == "off"
    assert pr_mode == "enforce"
    assert pr_policy["merge"] == "never"
    assert pr_policy["early_draft_pr"] is True


def test_direct_closeout_rejects_verification_followed_by_mutation(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    canonical = tmp_path / "canonical"
    mutable = tmp_path / "mutable"
    _init_repo(canonical)
    _run(canonical, "worktree", "add", "-b", "discord/action-test", str(mutable), "HEAD")
    (mutable / "scripts").mkdir()
    (mutable / "scripts" / "run_tests.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    _run(mutable, "add", "scripts/run_tests.sh")
    _run(mutable, "commit", "-m", "add test wrapper")
    runner, captured, notifications = _direct_closeout_runner(mutable)

    evidence = record_terminal_result(
        command="scripts/run_tests.sh && mutate && git commit -am unsafe",
        cwd=mutable,
        session_id="direct-closeout-composite",
        exit_code=0,
        output="passed",
    )
    result = {"runtime_breakdown": {"verification_evidence": [evidence] if evidence else []}}

    assert evidence is None
    assert runner._activate_direct_closeout_after_checkpoint("work-1", result) is None
    assert captured == {}
    assert notifications == []


def test_direct_closeout_does_not_activate_without_verification(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    canonical = tmp_path / "canonical"
    mutable = tmp_path / "mutable"
    _init_repo(canonical)
    _run(canonical, "worktree", "add", "-b", "discord/action-test", str(mutable), "HEAD")
    runner, captured, notifications = _direct_closeout_runner(mutable)

    assert runner._activate_direct_closeout_after_checkpoint("work-1", {}) is None
    assert captured == {}
    assert notifications == []


@pytest.mark.parametrize(
    "missing_field",
    [
        "repository_root",
        "canonical_command",
        "scope",
        "mutation_generation",
        "mutation_boundary",
        "verified_head_sha",
    ],
)
def test_direct_closeout_rejects_incomplete_trusted_verification_evidence(
    tmp_path,
    monkeypatch,
    missing_field,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    canonical = tmp_path / "canonical"
    mutable = tmp_path / "mutable"
    _init_repo(canonical)
    _run(canonical, "worktree", "add", "-b", "discord/action-test", str(mutable), "HEAD")
    runner, captured, notifications = _direct_closeout_runner(mutable)
    result = _successful_verification_result(mutable)
    result["runtime_breakdown"]["verification_evidence"][0].pop(missing_field)

    assert runner._activate_direct_closeout_after_checkpoint("work-1", result) is None
    assert captured == {}
    assert notifications == []


def test_direct_closeout_rejects_evidence_from_other_root_or_mutation_boundary(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    canonical = tmp_path / "canonical"
    mutable = tmp_path / "mutable"
    _init_repo(canonical)
    _run(canonical, "worktree", "add", "-b", "discord/action-test", str(mutable), "HEAD")
    runner, captured, notifications = _direct_closeout_runner(mutable)

    wrong_root = _successful_verification_result(mutable)
    wrong_root["runtime_breakdown"]["verification_evidence"][0]["repository_root"] = str(canonical)
    assert runner._activate_direct_closeout_after_checkpoint("work-1", wrong_root) is None

    stale_mutation = _successful_verification_result(
        mutable,
        mutation_generation=1,
        mutation_boundary=2,
    )
    stale_mutation["runtime_breakdown"]["mutation_generation"] = 2
    stale_mutation["runtime_breakdown"]["mutation_boundary"] = 3
    assert runner._activate_direct_closeout_after_checkpoint("work-1", stale_mutation) is None
    assert captured == {}
    assert notifications == []


def test_direct_closeout_never_infers_verification_from_later_head(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    canonical = tmp_path / "canonical"
    mutable = tmp_path / "mutable"
    _init_repo(canonical)
    _run(canonical, "worktree", "add", "-b", "discord/action-test", str(mutable), "HEAD")
    runner, captured, notifications = _direct_closeout_runner(mutable)
    result = _successful_verification_result(mutable)

    (mutable / "after-verification.txt").write_text("later head\n", encoding="utf-8")
    _run(mutable, "add", "after-verification.txt")
    _run(mutable, "commit", "-m", "later head")

    assert runner._activate_direct_closeout_after_checkpoint("work-1", result) is None
    assert captured == {}
    assert notifications == []


def test_direct_closeout_does_not_activate_dirty_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    canonical = tmp_path / "canonical"
    mutable = tmp_path / "mutable"
    _init_repo(canonical)
    _run(canonical, "worktree", "add", "-b", "discord/action-test", str(mutable), "HEAD")
    (mutable / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
    runner, captured, notifications = _direct_closeout_runner(mutable, mode="enforce")

    assert runner._activate_direct_closeout_after_checkpoint(
        "work-1",
        _successful_verification_result(mutable),
    ) is None
    assert captured == {}
    assert notifications == []


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
@pytest.mark.parametrize("source", ["direct", "fable"])
def test_direct_closeout_activates_clean_verified_exact_head(
    tmp_path,
    monkeypatch,
    mode,
    source,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    canonical = tmp_path / "canonical"
    mutable = tmp_path / "mutable"
    _init_repo(canonical)
    _run(canonical, "worktree", "add", "-b", "discord/action-test", str(mutable), "HEAD")
    runner, captured, notifications = _direct_closeout_runner(
        mutable,
        mode=mode,
        source=source,
    )
    head_sha = _run(mutable, "rev-parse", "HEAD").stdout.strip()

    activated = runner._activate_direct_closeout_after_checkpoint(
        "work-1",
        _successful_verification_result(mutable),
    )

    assert activated is not None
    assert captured["expected_revision"] == 4
    assert captured["state"]["mode"] == mode
    assert captured["state"]["local_verification"] == {
        "status": "passed",
        "head_sha": head_sha,
    }
    assert captured["state"]["pr"]["head_sha"] == head_sha
    assert captured["state"]["ci"]["head_sha"] == head_sha
    assert notifications == ["work-1"]


def _required_closeout_state(head_sha: str, repo: Path) -> dict:
    return {
        "dispatches": {
            "deleg-required": {
                "delegation_id": "deleg-required",
                "completed_at": 1.0,
                "evidence": {
                    "head_sha": head_sha,
                    "base_sha": head_sha,
                    "worker_cwd": str(repo),
                    "scope_paths": ["allowed"],
                    "scope_check": {
                        "clean": True,
                        "out_of_scope_files": [],
                        "scope_paths": ["allowed"],
                    },
                },
            }
        }
    }


def _durable_required_closeout_runner(
    tmp_path: Path,
    workspace: Path,
    *,
    base_sha: str,
    scope_paths: list[str],
) -> tuple[gateway_run.GatewayRunner, GatewayWorkLedger, str]:
    ledger = GatewayWorkLedger(tmp_path / "durable-required-closeout.json")
    event = MessageEvent(
        text="implement the scoped change",
        source=_source(workspace),
        message_id="durable-required-closeout",
    )
    session_key = "agent:main:discord:thread:thread-123"
    item = ledger.accept_event(
        event,
        session_key=session_key,
        freshness_seconds=60,
    )
    assert item is not None
    assert ledger.mark_agent_running(
        item["id"],
        session_key=session_key,
        run_generation=7,
        owner_pid=os.getpid(),
        process_epoch="checkpoint-test",
    )
    identity = {
        "generation": 7,
        "attempt_id": "checkpoint-test:7",
        "attempt_order": 10,
    }
    assert ledger.begin_required_async_attempt(item["id"], **identity)
    assert ledger.register_required_async_dispatch(
        item["id"],
        delegation_id="worker-checkpoint",
        owner_pid=os.getpid(),
        process_epoch="checkpoint-test",
        scope_paths=scope_paths,
        **identity,
    )
    assert ledger.record_required_async_completion(
        item["id"],
        delegation_id="worker-checkpoint",
        success=True,
        status="completed",
        evidence={
            "base_sha": base_sha,
            "worker_cwd": str(workspace),
            "scope_paths": scope_paths,
            "scope_check": {"clean": True, "scope_paths": scope_paths},
        },
        **identity,
    )
    assert ledger.seal_required_async_attempt(item["id"], **identity)
    assert ledger.attach_closeout_workspace(
        item["id"],
        workspace_path=str(workspace),
        source="direct",
        mode="enforce",
        policy={"require_local_verification": True},
    )
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.work_ledger = ledger
    runner.trusted_closeout_watcher = SimpleNamespace(notify=lambda _work_id: None)
    return runner, ledger, item["id"]


def test_required_closeout_activates_clean_matching_host_head(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    _init_repo(repo)
    runner, captured, notifications = _direct_closeout_runner(
        repo,
        mode="enforce",
    )
    head_sha = _run(repo, "rev-parse", "HEAD").stdout.strip()

    activated, route = runner._activate_required_async_closeout(
        "work-1",
        runner.work_ledger.get("work-1"),
        _required_closeout_state(head_sha, repo),
    )

    assert (activated, route) == (True, "closeout")
    assert captured["state"]["local_verification"] == {
        "status": "passed",
        "head_sha": head_sha,
    }
    assert notifications == ["work-1"]


def test_required_closeout_rejects_mismatched_host_head(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    _init_repo(repo)
    runner, captured, notifications = _direct_closeout_runner(
        repo,
        mode="enforce",
    )

    activated, route = runner._activate_required_async_closeout(
        "work-1",
        runner.work_ledger.get("work-1"),
        _required_closeout_state("a" * 40, repo),
    )

    assert (activated, route) == (
        False,
        "checkpoint_base_head_moved",
    )
    assert captured == {}
    assert notifications == []


def test_required_closeout_rejects_dirty_host_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    _init_repo(repo)
    head_sha = _run(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
    runner, captured, notifications = _direct_closeout_runner(
        repo,
        mode="enforce",
    )

    activated, route = runner._activate_required_async_closeout(
        "work-1",
        runner.work_ledger.get("work-1"),
        _required_closeout_state(head_sha, repo),
    )

    assert (activated, route) == (
        False,
        "checkpoint_changed_paths_out_of_scope",
    )
    assert captured == {}
    assert notifications == []


def test_required_closeout_checkpoints_union_scoped_async_mutation(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    _init_repo(repo)
    base_sha = _run(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "src").mkdir()
    (repo / "tests").mkdir()
    (repo / "src" / "parser.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "tests" / "test_parser.py").write_text("def test_value(): pass\n", encoding="utf-8")
    runner, captured, notifications = _direct_closeout_runner(repo, mode="enforce")
    state = {
        "dispatches": {
            "worker-a": {
                "required": True,
                "completed_at": 1.0,
                "evidence": {
                    "base_sha": base_sha,
                    "worker_cwd": str(repo),
                    "scope_paths": ["src"],
                    "scope_check": {"clean": True, "scope_paths": ["src"]},
                },
            },
            "worker-b": {
                "required": True,
                "completed_at": 2.0,
                "evidence": {
                    "base_sha": base_sha,
                    "worker_cwd": str(repo),
                    "scope_paths": ["tests"],
                    "scope_check": {"clean": True, "scope_paths": ["tests"]},
                },
            },
        }
    }

    activated, route = runner._activate_required_async_closeout(
        "work-1",
        runner.work_ledger.get("work-1"),
        state,
    )

    assert (activated, route) == (True, "closeout")
    checkpoint_sha = _run(repo, "rev-parse", "HEAD").stdout.strip()
    assert checkpoint_sha != base_sha
    assert _run(repo, "status", "--porcelain").stdout == ""
    assert captured["state"]["local_verification"]["head_sha"] == checkpoint_sha
    assert notifications == ["work-1"]


def test_required_closeout_checkpoints_merged_sibling_worktree_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    worker = tmp_path / "repo-pw-live-case"
    _init_repo(repo)
    base_sha = _run(repo, "rev-parse", "HEAD").stdout.strip()
    _run(repo, "worktree", "add", "-b", "worker/live", str(worker), "HEAD")
    (repo / "src").mkdir()
    (repo / "src" / "live.py").write_text("READY = True\n", encoding="utf-8")
    runner, captured, _notifications = _direct_closeout_runner(repo, mode="enforce")
    state = {
        "dispatches": {
            "worker-a": {
                "required": True,
                "completed_at": 1.0,
                "evidence": {
                    "base_sha": base_sha,
                    "worker_cwd": str(worker),
                    "scope_paths": ["src"],
                    "scope_check": {"clean": True, "scope_paths": ["src"]},
                    "parallel_merge": {"merged": True, "success": True},
                },
            }
        }
    }

    activated, route = runner._activate_required_async_closeout(
        "work-1",
        runner.work_ledger.get("work-1"),
        state,
    )

    assert (activated, route) == (True, "closeout")
    assert captured["state"]["local_verification"]["status"] == "passed"


def test_required_closeout_refuses_checkpoint_from_dirty_baseline(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    _init_repo(repo)
    base_sha = _run(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "src").mkdir()
    (repo / "src" / "parser.py").write_text("VALUE = 1\n", encoding="utf-8")
    runner, captured, notifications = _direct_closeout_runner(repo, mode="enforce")
    state = {
        "dispatches": {
            "worker-a": {
                "required": True,
                "completed_at": 1.0,
                "evidence": {
                    "base_sha": base_sha,
                    "initial_dirty_paths": ["unrelated.txt"],
                    "worker_cwd": str(repo),
                    "scope_paths": ["src"],
                    "scope_check": {"clean": True, "scope_paths": ["src"]},
                },
            }
        }
    }

    activated, route = runner._activate_required_async_closeout(
        "work-1",
        runner.work_ledger.get("work-1"),
        state,
    )

    assert (activated, route) == (False, "checkpoint_baseline_was_dirty")
    assert captured == {}
    assert notifications == []


def test_required_closeout_replays_exact_checkpoint_after_activation_crash(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    _init_repo(repo)
    base_sha = _run(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "src").mkdir()
    (repo / "src" / "parser.py").write_text("VALUE = 1\n", encoding="utf-8")
    runner, ledger, work_id = _durable_required_closeout_runner(
        tmp_path,
        repo,
        base_sha=base_sha,
        scope_paths=["src"],
    )
    state = ledger.required_async_completion_state(work_id)
    item = ledger.get(work_id)
    original_activate = runner._activate_closeout_at_verified_head
    monkeypatch.setattr(
        runner,
        "_activate_closeout_at_verified_head",
        MagicMock(side_effect=RuntimeError("crash after checkpoint commit")),
    )

    with pytest.raises(RuntimeError, match="crash after checkpoint commit"):
        runner._activate_required_async_closeout(work_id, item, state)

    checkpoint_head = _run(repo, "rev-parse", "HEAD").stdout.strip()
    assert checkpoint_head != base_sha
    persisted = ledger.required_async_completion_state(work_id)
    assert persisted["checkpoint"]["parent_sha"] == base_sha
    assert persisted["checkpoint"]["committed_head_sha"] == checkpoint_head

    restarted = object.__new__(gateway_run.GatewayRunner)
    restarted.work_ledger = ledger
    restarted.trusted_closeout_watcher = SimpleNamespace(notify=lambda _work_id: None)
    monkeypatch.setattr(
        restarted,
        "_activate_closeout_at_verified_head",
        original_activate,
    )
    activated, route = restarted._activate_required_async_closeout(
        work_id,
        ledger.get(work_id),
        persisted,
    )

    assert (activated, route) == (True, "closeout")
    assert ledger.get(work_id)["closeout_authoritative"] is True


def test_required_closeout_recovers_crash_after_ref_cas_before_index_replace(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    _init_repo(repo)
    base_sha = _run(repo, "rev-parse", "HEAD").stdout.strip()
    base_tree = _run(repo, "rev-parse", f"{base_sha}^{{tree}}").stdout.strip()
    (repo / "src").mkdir()
    (repo / "src" / "parser.py").write_text("VALUE = 1\n", encoding="utf-8")
    runner, ledger, work_id = _durable_required_closeout_runner(
        tmp_path,
        repo,
        base_sha=base_sha,
        scope_paths=["src"],
    )
    real_replace = os.replace
    crashed = False
    index_path = (repo / ".git" / "index").resolve()

    def crash_before_index_replace(source, destination):
        nonlocal crashed
        destination_path = Path(destination).resolve()
        if not crashed and destination_path == index_path:
            crashed = True
            raise RuntimeError("crash after update-ref")
        return real_replace(source, destination)

    monkeypatch.setattr(os, "replace", crash_before_index_replace)
    with pytest.raises(RuntimeError, match="crash after update-ref"):
        runner._activate_required_async_closeout(
            work_id,
            ledger.get(work_id),
            ledger.required_async_completion_state(work_id),
        )

    checkpoint_head = _run(repo, "rev-parse", "HEAD").stdout.strip()
    assert checkpoint_head != base_sha
    assert _run(repo, "write-tree").stdout.strip() == base_tree
    assert _run(repo, "status", "--porcelain").stdout
    persisted = ledger.required_async_completion_state(work_id)
    assert "committed_head_sha" not in persisted["checkpoint"]

    restarted = object.__new__(gateway_run.GatewayRunner)
    restarted.work_ledger = ledger
    restarted.trusted_closeout_watcher = SimpleNamespace(notify=lambda _work_id: None)
    replay_result = restarted._activate_required_async_closeout(
        work_id,
        ledger.get(work_id),
        persisted,
    )
    assert replay_result == (True, "closeout"), replay_result
    assert _run(repo, "status", "--porcelain").stdout == ""
    recovered = ledger.required_async_completion_state(work_id)
    assert recovered["checkpoint"]["committed_head_sha"] == checkpoint_head


def test_required_closeout_ref_cas_loss_never_moves_competing_head(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    _init_repo(repo)
    base_sha = _run(repo, "rev-parse", "HEAD").stdout.strip()
    base_tree = _run(repo, "rev-parse", f"{base_sha}^{{tree}}").stdout.strip()
    (repo / "src").mkdir()
    (repo / "src" / "parser.py").write_text("VALUE = 1\n", encoding="utf-8")
    runner, _captured, _notifications = _direct_closeout_runner(repo, mode="enforce")
    state = {
        "dispatches": {
            "worker-a": {
                "required": True,
                "completed_at": 1.0,
                "evidence": {
                    "base_sha": base_sha,
                    "worker_cwd": str(repo),
                    "scope_paths": ["src"],
                    "scope_check": {"clean": True, "scope_paths": ["src"]},
                },
            }
        }
    }
    original_run = subprocess.run
    competing_head = ""
    checkpoint_head = ""
    injected = False

    def racing_ref(*args, **kwargs):
        nonlocal competing_head, checkpoint_head, injected
        command = args[0] if args else kwargs.get("args")
        if (
            not injected
            and isinstance(command, list)
            and "update-ref" in command
            and "HEAD" in command
        ):
            injected = True
            checkpoint_head = command[-2]
            competitor = original_run(
                ["git", "commit-tree", base_tree, "-p", base_sha],
                cwd=repo,
                input="competing commit\n",
                capture_output=True,
                text=True,
                check=True,
            )
            competing_head = competitor.stdout.strip()
            original_run(
                ["git", "update-ref", "HEAD", competing_head, base_sha],
                cwd=repo,
                capture_output=True,
                text=True,
                check=True,
            )
        return original_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", racing_ref)

    assert runner._activate_required_async_closeout(
        "work-1",
        runner.work_ledger.get("work-1"),
        state,
    ) == (False, "checkpoint_ref_cas_lost")
    assert _run(repo, "rev-parse", "HEAD").stdout.strip() == competing_head
    assert _run(repo, "show", "-s", "--format=%P", checkpoint_head).stdout.strip() == base_sha
    assert _run(repo, "show", "-s", "--format=%T", checkpoint_head).stdout.strip() != base_tree


def test_required_closeout_private_index_excludes_concurrent_real_index_mutation(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    _init_repo(repo)
    base_sha = _run(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "src").mkdir()
    (repo / "src" / "parser.py").write_text("VALUE = 1\n", encoding="utf-8")
    runner, _captured, _notifications = _direct_closeout_runner(repo, mode="enforce")
    state = {
        "dispatches": {
            "worker-a": {
                "required": True,
                "completed_at": 1.0,
                "evidence": {
                    "base_sha": base_sha,
                    "worker_cwd": str(repo),
                    "scope_paths": ["src"],
                    "scope_check": {"clean": True, "scope_paths": ["src"]},
                },
            }
        }
    }
    original_run = subprocess.run
    checkpoint_head = ""
    injected = False

    def mutate_real_index(*args, **kwargs):
        nonlocal checkpoint_head, injected
        result = original_run(*args, **kwargs)
        command = args[0] if args else kwargs.get("args")
        if (
            not injected
            and isinstance(command, list)
            and "commit-tree" in command
            and result.returncode == 0
        ):
            injected = True
            checkpoint_head = str(result.stdout or "").strip()
            (repo / "outside.txt").write_text("unrelated\n", encoding="utf-8")
            alternate = repo / ".git" / "adversary-index"
            env = {**os.environ, "GIT_INDEX_FILE": str(alternate)}
            original_run(
                ["git", "read-tree", base_sha],
                cwd=repo,
                env=env,
                check=True,
            )
            original_run(
                ["git", "add", "outside.txt"],
                cwd=repo,
                env=env,
                check=True,
            )
            (repo / ".git" / "index").write_bytes(alternate.read_bytes())
            alternate.unlink()
        return result

    monkeypatch.setattr(subprocess, "run", mutate_real_index)

    assert runner._activate_required_async_closeout(
        "work-1",
        runner.work_ledger.get("work-1"),
        state,
    ) == (False, "checkpoint_real_index_changed")
    assert _run(repo, "rev-parse", "HEAD").stdout.strip() == base_sha
    assert _run(repo, "show", f"{checkpoint_head}:src/parser.py").stdout == "VALUE = 1\n"
    missing = subprocess.run(
        ["git", "show", f"{checkpoint_head}:outside.txt"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert missing.returncode != 0


def test_required_closeout_cancellation_after_intent_prevents_commit_and_ref(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    _init_repo(repo)
    base_sha = _run(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "src").mkdir()
    (repo / "src" / "parser.py").write_text("VALUE = 1\n", encoding="utf-8")
    runner, ledger, work_id = _durable_required_closeout_runner(
        tmp_path,
        repo,
        base_sha=base_sha,
        scope_paths=["src"],
    )
    original_record = ledger.record_required_async_checkpoint
    cancelled = False

    def record_then_cancel(*args, **kwargs):
        nonlocal cancelled
        result = original_record(*args, **kwargs)
        if not cancelled and not kwargs.get("committed_head_sha"):
            cancelled = True
            identity = runner._required_async_identity(result)
            ledger.cancel_required_async_attempt(
                work_id,
                **identity,
                reason="session_stop",
            )
        return result

    monkeypatch.setattr(ledger, "record_required_async_checkpoint", record_then_cancel)

    assert runner._activate_required_async_closeout(
        work_id,
        ledger.get(work_id),
        ledger.required_async_completion_state(work_id),
    ) == (False, "checkpoint_attempt_cancelled")
    assert _run(repo, "rev-parse", "HEAD").stdout.strip() == base_sha
    assert _run(repo, "rev-list", "--count", "--all").stdout.strip() == "1"
    assert ledger.required_async_completion_state(work_id)["attempt_cancelled"] is True


def test_required_closeout_rejects_worker_created_clean_commit(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    _init_repo(repo)
    base_sha = _run(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "src").mkdir()
    (repo / "src" / "worker.py").write_text("WORKER = True\n", encoding="utf-8")
    _run(repo, "add", "src/worker.py")
    _run(repo, "commit", "-m", "worker-created commit")
    runner, captured, notifications = _direct_closeout_runner(repo, mode="enforce")
    state = {
        "dispatches": {
            "worker-a": {
                "required": True,
                "completed_at": 1.0,
                "evidence": {
                    "base_sha": base_sha,
                    "worker_cwd": str(repo),
                    "scope_paths": ["src"],
                    "scope_check": {"clean": True, "scope_paths": ["src"]},
                },
            }
        }
    }

    assert runner._activate_required_async_closeout(
        "work-1",
        runner.work_ledger.get("work-1"),
        state,
    ) == (False, "checkpoint_base_head_moved")
    assert captured == {}
    assert notifications == []


def test_required_closeout_exact_staging_handles_delete_and_rename(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "src").mkdir()
    (repo / "src" / "delete.py").write_text("DELETE = True\n", encoding="utf-8")
    (repo / "src" / "old.py").write_text("OLD = True\n", encoding="utf-8")
    _run(repo, "add", "src")
    _run(repo, "commit", "-m", "add scoped files")
    base_sha = _run(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "src" / "delete.py").unlink()
    (repo / "src" / "old.py").rename(repo / "src" / "renamed.py")
    runner, _captured, _notifications = _direct_closeout_runner(repo, mode="enforce")
    state = {
        "dispatches": {
            "worker-a": {
                "required": True,
                "completed_at": 1.0,
                "evidence": {
                    "base_sha": base_sha,
                    "worker_cwd": str(repo),
                    "scope_paths": ["src"],
                    "scope_check": {"clean": True, "scope_paths": ["src"]},
                },
            }
        }
    }

    assert runner._activate_required_async_closeout(
        "work-1",
        runner.work_ledger.get("work-1"),
        state,
    ) == (True, "closeout")
    assert not (repo / "src" / "delete.py").exists()
    assert not (repo / "src" / "old.py").exists()
    assert (repo / "src" / "renamed.py").exists()
    assert _run(repo, "status", "--porcelain").stdout == ""


def test_required_closeout_fences_concurrent_out_of_scope_staging(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    _init_repo(repo)
    base_sha = _run(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "src").mkdir()
    (repo / "src" / "parser.py").write_text("VALUE = 1\n", encoding="utf-8")
    runner, captured, notifications = _direct_closeout_runner(repo, mode="enforce")
    state = {
        "dispatches": {
            "worker-a": {
                "required": True,
                "completed_at": 1.0,
                "evidence": {
                    "base_sha": base_sha,
                    "worker_cwd": str(repo),
                    "scope_paths": ["src"],
                    "scope_check": {"clean": True, "scope_paths": ["src"]},
                },
            }
        }
    }
    original_run = subprocess.run
    injected = False

    def racing_run(*args, **kwargs):
        nonlocal injected
        result = original_run(*args, **kwargs)
        command = args[0] if args else kwargs.get("args")
        if (
            not injected
            and isinstance(command, list)
            and "add" in command
            and "-A" in command
        ):
            injected = True
            (repo / "outside.txt").write_text("concurrent\n", encoding="utf-8")
            original_run(
                ["git", "add", "outside.txt"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )
        return result

    monkeypatch.setattr(subprocess, "run", racing_run)

    assert runner._activate_required_async_closeout(
        "work-1",
        runner.work_ledger.get("work-1"),
        state,
    ) == (False, "checkpoint_workspace_changed_after_stage")
    assert _run(repo, "rev-parse", "HEAD").stdout.strip() == base_sha
    assert "outside.txt" in _run(repo, "diff", "--cached", "--name-only").stdout
    assert captured == {}
    assert notifications == []


def test_required_closeout_disables_hooks_and_commit_signing(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    _init_repo(repo)
    base_sha = _run(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "src").mkdir()
    (repo / "src" / "parser.py").write_text("VALUE = 1\n", encoding="utf-8")
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text(
        "#!/bin/sh\nprintf 'ran\\n' > hook-ran.txt\ngit add hook-ran.txt\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    _run(repo, "config", "commit.gpgSign", "true")
    _run(repo, "config", "user.signingkey", "definitely-missing")
    _run(repo, "config", "gpg.program", "/bin/false")
    runner, _captured, _notifications = _direct_closeout_runner(repo, mode="enforce")
    state = {
        "dispatches": {
            "worker-a": {
                "required": True,
                "completed_at": 1.0,
                "evidence": {
                    "base_sha": base_sha,
                    "worker_cwd": str(repo),
                    "scope_paths": ["src"],
                    "scope_check": {"clean": True, "scope_paths": ["src"]},
                },
            }
        }
    }

    assert runner._activate_required_async_closeout(
        "work-1",
        runner.work_ledger.get("work-1"),
        state,
    ) == (True, "closeout")
    assert not (repo / "hook-ran.txt").exists()
    assert _run(repo, "status", "--porcelain").stdout == ""


def test_required_closeout_nested_workspace_uses_git_toplevel(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    _init_repo(repo)
    nested = repo / "packages" / "app"
    nested.mkdir(parents=True)
    (nested / "baseline.txt").write_text("baseline\n", encoding="utf-8")
    _run(repo, "add", "packages/app/baseline.txt")
    _run(repo, "commit", "-m", "add nested workspace")
    base_sha = _run(repo, "rev-parse", "HEAD").stdout.strip()
    (nested / "src").mkdir()
    (nested / "src" / "app.py").write_text("READY = True\n", encoding="utf-8")
    runner, captured, notifications = _direct_closeout_runner(nested, mode="enforce")
    state = {
        "dispatches": {
            "worker-a": {
                "required": True,
                "completed_at": 1.0,
                "evidence": {
                    "base_sha": base_sha,
                    "worker_cwd": str(nested),
                    "scope_paths": ["src"],
                    "scope_check": {"clean": True, "scope_paths": ["src"]},
                },
            }
        }
    }

    assert runner._activate_required_async_closeout(
        "work-1",
        runner.work_ledger.get("work-1"),
        state,
    ) == (True, "closeout")
    assert captured["state"]["local_verification"]["status"] == "passed"
    assert _run(repo, "status", "--porcelain").stdout == ""
    assert notifications == ["work-1"]


def test_direct_closeout_accepts_git_toplevel_for_nested_workspace(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    canonical = tmp_path / "canonical"
    mutable = tmp_path / "mutable"
    _init_repo(canonical)
    _run(
        canonical,
        "worktree",
        "add",
        "-b",
        "discord/action-test",
        str(mutable),
        "HEAD",
    )
    nested = mutable / "packages" / "app"
    nested.mkdir(parents=True)
    (nested / "app.txt").write_text("nested workspace\n", encoding="utf-8")
    _run(mutable, "add", "packages/app/app.txt")
    _run(mutable, "commit", "-m", "add nested workspace")
    runner, captured, notifications = _direct_closeout_runner(nested, mode="enforce")
    head_sha = _run(mutable, "rev-parse", "HEAD").stdout.strip()

    activated = runner._activate_direct_closeout_after_checkpoint(
        "work-1",
        _successful_verification_result(mutable),
    )

    assert activated is not None
    assert captured["state"]["local_verification"] == {
        "status": "passed",
        "head_sha": head_sha,
    }
    assert notifications == ["work-1"]


def test_direct_closeout_binds_required_visual_receipt_to_exact_head(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    canonical = tmp_path / "canonical"
    mutable = tmp_path / "mutable"
    _init_repo(canonical)
    _run(canonical, "worktree", "add", "-b", "discord/action-test", str(mutable), "HEAD")
    runner, captured, _notifications = _direct_closeout_runner(
        mutable,
        mode="enforce",
        require_visual_qa=True,
    )
    monkeypatch.setattr(
        "agent.visual_qa.visual_receipt_completion",
        lambda requirement, receipts, *, min_order: {"status": "passed"},
    )
    result = _successful_verification_result(mutable)
    result["visual_qa"] = {"receipts": [{"status": "passed"}], "min_receipt_order": 1}
    head_sha = _run(mutable, "rev-parse", "HEAD").stdout.strip()

    assert runner._activate_direct_closeout_after_checkpoint("work-1", result) is not None
    assert captured["state"]["visual_qa"] == {
        "status": "passed",
        "head_sha": head_sha,
    }


def test_direct_closeout_activates_visual_pending_before_followup(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    canonical = tmp_path / "canonical"
    mutable = tmp_path / "mutable"
    _init_repo(canonical)
    _run(canonical, "worktree", "add", "-b", "discord/action-test", str(mutable), "HEAD")
    runner, captured, notifications = _direct_closeout_runner(
        mutable,
        mode="enforce",
        require_visual_qa=True,
    )
    head_sha = _run(mutable, "rev-parse", "HEAD").stdout.strip()

    activated = runner._activate_direct_closeout_after_checkpoint(
        "work-1",
        _successful_verification_result(mutable),
        visual_pending=True,
    )

    assert activated is not None
    assert captured["state"]["visual_qa"] == {
        "status": "pending",
        "head_sha": head_sha,
    }
    assert notifications == ["work-1"]


def test_final_direct_closeout_applies_visual_to_latest_activated_revision(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    canonical = tmp_path / "canonical"
    mutable = tmp_path / "mutable"
    _init_repo(canonical)
    _run(canonical, "worktree", "add", "-b", "discord/action-test", str(mutable), "HEAD")
    runner, captured, notifications = _direct_closeout_runner(
        mutable,
        mode="enforce",
        require_visual_qa=True,
        activated=True,
    )
    result = _successful_verification_result(mutable)
    result["visual_qa"] = {
        "receipts": [{"status": "passed"}],
        "min_receipt_order": 5,
    }
    head_sha = _run(mutable, "rev-parse", "HEAD").stdout.strip()

    applied = runner._activate_direct_closeout_after_checkpoint("work-1", result)

    assert applied is not None
    assert captured["applied_head_sha"] == head_sha
    assert captured["min_receipt_order"] == 5
    assert applied["status"] == "waiting_for_ci"
    assert applied["pr"]["url"].endswith("/1")
    assert applied["visual_qa"] == {"status": "passed", "head_sha": head_sha}
    assert notifications == ["work-1"]


def test_final_direct_closeout_publishes_new_verified_head_before_visual(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    canonical = tmp_path / "canonical"
    mutable = tmp_path / "mutable"
    _init_repo(canonical)
    _run(canonical, "worktree", "add", "-b", "discord/action-test", str(mutable), "HEAD")
    runner, captured, notifications = _direct_closeout_runner(
        mutable,
        mode="enforce",
        require_visual_qa=True,
        activated=True,
    )
    first_head_sha = _run(mutable, "rev-parse", "HEAD").stdout.strip()
    (mutable / "visual-correction.txt").write_text("corrected\n", encoding="utf-8")
    _run(mutable, "add", "visual-correction.txt")
    _run(mutable, "commit", "-m", "visual correction")
    second_head_sha = _run(mutable, "rev-parse", "HEAD").stdout.strip()
    result = _successful_verification_result(mutable)
    result["visual_qa"] = {
        "receipts": [{"status": "passed"}],
        "min_receipt_order": 8,
    }

    applied = runner._activate_direct_closeout_after_checkpoint("work-1", result)

    assert applied is not None
    assert captured["published_from_head_sha"] == first_head_sha
    assert captured["published_head_sha"] == second_head_sha
    assert captured["applied_head_sha"] == second_head_sha
    assert applied["visual_qa"] == {"status": "passed", "head_sha": second_head_sha}
    assert notifications == ["work-1", "work-1"]


def test_final_fable_closeout_applies_parent_visual_to_published_exact_head(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    canonical = tmp_path / "canonical"
    mutable = tmp_path / "mutable"
    _init_repo(canonical)
    _run(canonical, "worktree", "add", "-b", "discord/action-test", str(mutable), "HEAD")
    runner, captured, notifications = _direct_closeout_runner(
        mutable,
        mode="enforce",
        require_visual_qa=True,
        activated=True,
        source="fable",
    )
    first_head_sha = _run(mutable, "rev-parse", "HEAD").stdout.strip()
    (mutable / "fable-correction.txt").write_text("corrected\n", encoding="utf-8")
    _run(mutable, "add", "fable-correction.txt")
    _run(mutable, "commit", "-m", "fable correction")
    result = _successful_verification_result(mutable)
    result["visual_qa"] = {
        "receipts": [{"status": "passed"}],
        "min_receipt_order": 7,
    }
    head_sha = _run(mutable, "rev-parse", "HEAD").stdout.strip()

    applied = runner._activate_direct_closeout_after_checkpoint("work-1", result)

    assert applied is not None
    assert captured["published_from_head_sha"] == first_head_sha
    assert captured["published_head_sha"] == head_sha
    assert captured["applied_head_sha"] == head_sha
    assert captured["min_receipt_order"] == 7
    assert applied["visual_qa"] == {"status": "passed", "head_sha": head_sha}
    assert notifications == ["work-1", "work-1"]


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


def _accept_enforced_visual_action_item(runner, event, session_key):
    item = runner.work_ledger.accept_event(
        event,
        session_key=session_key,
        freshness_seconds=60,
        visual_qa_config={"mode": "enforce_explicit"},
    )
    assert item is not None
    event.work_item_id = item["id"]
    event.visual_qa_config = item["visual_qa_config"]
    event.visual_qa_requirement = item["visual_qa_requirement"]
    runner._discord_work_item_id_for_event = lambda *_args, **_kwargs: item["id"]
    runner._activate_direct_closeout_after_checkpoint = lambda *_args, **_kwargs: None
    runner._session_has_pending_background_workers = lambda *_args, **_kwargs: False
    return item


@pytest.mark.asyncio
async def test_enforced_visual_gate_prefixes_actual_discord_delivery_response(tmp_path):
    captured: dict = {}
    runner = _runner_for_action_turn(tmp_path, captured)
    runner.work_ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    source = _source(tmp_path)
    event = MessageEvent(
        text="Build a responsive dashboard with a mobile sidebar.",
        source=source,
        message_id="visual-blocked-response",
        discord_runtime_mode="action",
    )
    session_key = "agent:main:discord:thread:thread-123"
    item = _accept_enforced_visual_action_item(runner, event, session_key)
    runner._run_agent = AsyncMock(
        return_value={
            "final_response": "Fresh verification passed.",
            "messages": [
                {"role": "user", "content": event.text},
                {"role": "assistant", "content": "Fresh verification passed."},
            ],
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 0,
            "visual_qa": {
                "receipts": [],
                "code_mutation_observed": True,
                "min_receipt_order": 2,
            },
        }
    )

    response = await runner._handle_message_with_agent(
        event,
        source,
        session_key,
        1,
    )

    assert response.startswith("⚠️ **Completion blocked.** Enforced visual QA is active")
    assert "Fresh verification passed." in response
    assert "None" not in response
    stored = runner.work_ledger.get(item["id"])
    assert stored["completion_gate"]["allowed_to_complete"] is False
    assert stored["final_response"] == response


@pytest.mark.asyncio
async def test_enforced_visual_gate_sends_notice_after_streamed_discord_response(tmp_path):
    captured: dict = {}
    runner = _runner_for_action_turn(tmp_path, captured)
    runner.work_ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    adapter = SimpleNamespace(
        platform=Platform.DISCORD,
        _active_sessions={},
        send=AsyncMock(),
    )
    runner.adapters = {Platform.DISCORD: adapter}
    runner._update_discord_summaries = AsyncMock(return_value=True)
    source = _source(tmp_path)
    event = MessageEvent(
        text="Build a responsive dashboard with a mobile sidebar.",
        source=source,
        message_id="visual-blocked-stream",
        discord_runtime_mode="action",
    )
    session_key = "agent:main:discord:thread:thread-123"
    item = _accept_enforced_visual_action_item(runner, event, session_key)
    runner._run_agent = AsyncMock(
        return_value={
            "final_response": "Fresh verification passed.",
            "messages": [
                {"role": "user", "content": event.text},
                {"role": "assistant", "content": "Fresh verification passed."},
            ],
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 0,
            "already_sent": True,
            "visual_qa": {
                "receipts": [],
                "code_mutation_observed": True,
                "min_receipt_order": 2,
            },
        }
    )

    response = await runner._handle_message_with_agent(
        event,
        source,
        session_key,
        1,
    )

    assert response is None
    notice_calls = [
        call
        for call in adapter.send.await_args_list
        if len(call.args) > 1
        and str(call.args[1]).startswith("⚠️ **Completion blocked.**")
    ]
    assert len(notice_calls) == 1
    notice = notice_calls[0].args[1]
    assert notice.startswith("⚠️ **Completion blocked.** Enforced visual QA is active")
    assert "Fresh verification passed." not in notice
    assert "None" not in notice
    stored = runner.work_ledger.get(item["id"])
    assert stored["status"] == "blocked"
    assert stored["final_response"] == f"Fresh verification passed.\n\n{notice}"


@pytest.mark.asyncio
async def test_parent_exception_seals_released_required_worker_attempt(tmp_path):
    captured: dict = {}
    runner = _runner_for_action_turn(tmp_path, captured)
    runner._process_epoch = "1000-test"
    runner.work_ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    runner._resume_finished_discord_work_item = AsyncMock()
    source = _source(tmp_path)
    event = MessageEvent(
        text="Dispatch the worker, then fail.",
        source=source,
        message_id="parent-error-after-dispatch",
    )
    session_key = "agent:main:discord:thread:thread-123"
    item = runner.work_ledger.accept_event(
        event,
        session_key=session_key,
        freshness_seconds=60,
    )
    assert item is not None
    event.work_item_id = item["id"]
    runner._discord_work_item_id_for_event = lambda *_args, **_kwargs: item["id"]
    identity = runner._required_async_turn_identity(1)

    async def dispatch_then_raise(**_kwargs):
        assert runner.work_ledger.begin_required_async_attempt(
            item["id"],
            **identity,
        )
        assert runner.work_ledger.register_required_async_dispatch(
            item["id"],
            delegation_id="deleg-required",
            owner_pid=os.getpid(),
            process_epoch=runner._process_epoch,
            scope_paths=["src"],
            **identity,
        )
        assert runner.work_ledger.mark_required_async_dispatch_running(
            item["id"],
            delegation_id="deleg-required",
            owner_pid=os.getpid(),
            process_epoch=runner._process_epoch,
            **identity,
        )
        raise RuntimeError("parent failed after releasing worker")

    runner._run_agent = AsyncMock(side_effect=dispatch_then_raise)

    response = await runner._handle_message_with_agent(
        event,
        source,
        session_key,
        1,
    )

    assert "parent failed after releasing worker" in response
    state = runner.work_ledger.required_async_completion_state(item["id"])
    assert state["sealed"] is True
    assert state["all_terminal"] is False
    assert state["owns_recovery"] is True

    runner.work_ledger.record_required_async_completion(
        item["id"],
        delegation_id="deleg-required",
        success=False,
        status="error",
        error="worker failed after parent exit",
        **identity,
    )
    terminal = runner.work_ledger.required_async_completion_state(item["id"])
    assert terminal["ready_to_reconcile"] is True

    await runner._reconcile_required_async_item(item["id"], terminal)

    stored = runner.work_ledger.get(item["id"])
    assert stored["status"] == "blocked"
    runner._resume_finished_discord_work_item.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("fable_implementation", [False, True])
async def test_action_runtime_installs_visual_checkpoint_callback_for_fable_and_direct(
    tmp_path,
    monkeypatch,
    fable_implementation,
):
    import sys
    import types

    class CapturingAgent:
        instance = None

        def __init__(self, **_kwargs):
            self.tools = []
            type(self).instance = self

        def run_conversation(self, *_args, **_kwargs):
            return {
                "final_response": "done",
                "messages": [],
                "api_calls": 1,
                "model": "claude-fable-5" if fable_implementation else "gpt-5.6",
                "provider": "anthropic" if fable_implementation else "openai",
            }

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = CapturingAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda: {"tools": {"discord": {"enabled": ["all"]}}},
    )
    monkeypatch.setattr(
        "hermes_cli.tools_config._get_platform_tools",
        lambda _config, _platform: {"core", "terminal"},
    )

    captured: dict = {}
    runner = _runner_for_action_turn(tmp_path, captured)
    runner._run_agent = gateway_run.GatewayRunner._run_agent.__get__(
        runner,
        gateway_run.GatewayRunner,
    )
    model = "claude-fable-5" if fable_implementation else "gpt-5.6"
    provider = "anthropic" if fable_implementation else "openai"
    runtime = {"provider": provider, "api_key": "fake"}
    runner._resolve_session_agent_runtime = lambda **_kwargs: (model, runtime)
    runner._resolve_turn_agent_config = lambda *_args, **_kwargs: {
        "model": model,
        "runtime": runtime,
        "request_overrides": {},
    }
    runner._activate_direct_closeout_after_checkpoint = MagicMock(
        return_value={"status": "pending"}
    )
    source = _source(tmp_path)

    await runner._run_agent(
        message="Implement the responsive dashboard.",
        context_prompt="",
        history=[],
        source=source,
        session_id="session-1",
        session_key="agent:main:discord:thread:thread-123",
        feature_summary={"initial_request": "Implement the responsive dashboard."},
        discord_runtime_mode="action",
        fable_plan_metadata=(
            {
                "command": "fable",
                "fable_mode": "implementation",
            }
            if fable_implementation
            else None
        ),
        session_cwd_override=str(tmp_path),
        visual_qa_requirement={
            "level": "surface",
            "target": "dashboard",
            "assertions": ["no horizontal overflow"],
        },
        visual_qa_config={"mode": "enforce_explicit"},
        origin_work_item_id="work-1",
    )

    callback = CapturingAgent.instance._visual_qa_stop_callback
    assert callable(callback)
    callback({"mutation_generation": 1, "mutation_boundary": 2})
    runner._activate_direct_closeout_after_checkpoint.assert_called_once_with(
        "work-1",
        {
            "runtime_breakdown": {
                "mutation_generation": 1,
                "mutation_boundary": 2,
            }
        },
        visual_pending=True,
    )


@pytest.mark.asyncio
async def test_direct_agent_result_cas_does_not_overwrite_replacement_run(tmp_path):
    captured: dict = {}
    runner = _runner_for_action_turn(tmp_path, captured)
    runner.work_ledger = GatewayWorkLedger(
        tmp_path / "work_ledger.json",
        now_fn=lambda: 100.0,
    )
    source = _source(tmp_path)
    event = MessageEvent(
        text="Do the work",
        source=source,
        message_id="direct-result-race",
    )
    session_key = "agent:main:discord:thread:thread-123"
    item = runner.work_ledger.accept_event(
        event,
        session_key=session_key,
        freshness_seconds=60,
    )
    assert item is not None
    event.work_item_id = item["id"]
    runner._discord_work_item_id_for_event = lambda *_args, **_kwargs: item["id"]
    runner._activate_direct_closeout_after_checkpoint = lambda *_args, **_kwargs: None

    async def replace_run_before_result(**_kwargs):
        assert runner.work_ledger.mark_agent_running(
            item["id"],
            session_key=session_key,
            run_generation=2,
            owner_pid=os.getpid(),
            process_epoch="replacement-process",
        )
        return {
            "final_response": "stale direct result",
            "messages": [
                {"role": "user", "content": event.text},
                {"role": "assistant", "content": "stale direct result"},
            ],
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 0,
        }

    runner._run_agent = AsyncMock(side_effect=replace_run_before_result)

    response = await runner._handle_message_with_agent(
        event,
        source,
        session_key,
        1,
    )

    assert response is None
    stored = runner.work_ledger.get(item["id"])
    assert stored["status"] == "agent_running"
    assert stored["active_run"]["generation"] == 2
    assert stored["active_run"]["process_epoch"] == "replacement-process"
    assert "final_response" not in stored
    assert not hasattr(event, "work_item_run_state")


@pytest.mark.asyncio
async def test_successful_intake_escalation_queues_clean_action_turn_without_reprompt(
    tmp_path,
    monkeypatch,
):
    canonical_root = tmp_path / "canonical"
    canonical = canonical_root / "PID"
    _init_repo(canonical)
    _protect(monkeypatch, canonical_root)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: _config(canonical))

    captured: dict = {}
    runner = _runner_for_action_turn(tmp_path, captured)
    feature_summary = {
        "thread_id": "thread-123",
        "message_id": "summary-1",
        "initial_request": "Build the parser",
        "kanban_board": None,
    }
    promoted_event = MessageEvent(
        text="Build the parser",
        source=_source(canonical),
        message_id="message-1",
        feature_summary=feature_summary,
        discord_runtime_mode="action",
        internal=True,
    )
    adapter = SimpleNamespace(
        _active_sessions={},
        _pending_messages={},
        promote_event_to_action_request=AsyncMock(
            return_value=(promoted_event, "https://discord.com/channels/guild-1/thread-123")
        ),
        register_post_delivery_callback=lambda *args, **kwargs: None,
        send=AsyncMock(),
    )
    runner.adapters = {Platform.DISCORD: adapter}
    runner._session_has_pending_background_workers = lambda *args, **kwargs: False
    runner._discord_ledger_summary_status = lambda _work_id, status: status
    runner._register_discord_summary_post_delivery = (
        gateway_run.GatewayRunner._register_discord_summary_post_delivery.__get__(runner)
    )
    runner._session_key_for_source = (
        lambda _source: "agent:main:discord:thread:thread-123"
    )
    source = _source(canonical)
    event = MessageEvent(
        text="Can you explain this first?",
        source=source,
        message_id="message-1",
        discord_runtime_mode="read_only",
        discord_action_escalation_allowed=True,
    )
    async def _promote_then_stop(**_kwargs):
        return {
            "final_response": ".NO_REPLY",
            "messages": [
                {"role": "user", "content": event.text},
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "escalate-1",
                            "function": {
                                "name": "escalate_to_action",
                                "arguments": "{}",
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "escalate-1",
                    "content": '{"success": true, "action_escalation_requested": true}',
                },
            ],
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 0,
            "agent_persisted": False,
            "action_escalation_requested": {
                "success": True,
                "action_escalation_requested": True,
            },
        }

    runner._run_agent = AsyncMock(side_effect=_promote_then_stop)

    response = await runner._handle_message_with_agent(
        event,
        source,
        "agent:main:discord:thread:thread-123",
        1,
    )

    assert response is None
    assert event.source is source
    assert event.feature_summary is None
    assert adapter._pending_messages[
        "agent:main:discord:thread:thread-123"
    ] is promoted_event
    assert runner._run_agent.await_args.kwargs["defer_persistence"] is True
    runner.session_store.append_to_transcript.assert_not_called()


@pytest.mark.asyncio
async def test_read_only_turn_promotes_model_escalation_even_with_legacy_flag_false(
    tmp_path,
    monkeypatch,
):
    canonical_root = tmp_path / "canonical"
    canonical = canonical_root / "PID"
    _init_repo(canonical)
    _protect(monkeypatch, canonical_root)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: _config(canonical))

    captured: dict = {}
    runner = _runner_for_action_turn(tmp_path, captured)
    adapter = SimpleNamespace(
        _active_sessions={},
        _pending_messages={},
        promote_event_to_action_request=AsyncMock(),
        register_post_delivery_callback=lambda *args, **kwargs: None,
        send=AsyncMock(),
    )
    runner.adapters = {Platform.DISCORD: adapter}
    runner._session_has_pending_background_workers = lambda *args, **kwargs: False

    def _set_session_env(
        context,
        *,
        session_cwd="",
        discord_action_escalation_allowed=False,
    ):
        captured["escalation_allowed"] = discord_action_escalation_allowed
        return []

    runner._set_session_env = _set_session_env
    runner._run_agent = AsyncMock(
        return_value={
            "final_response": ".NO_REPLY",
            "messages": [],
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 0,
            "agent_persisted": False,
            "action_escalation_requested": {
                "success": True,
                "action_escalation_requested": True,
            },
        }
    )
    runner._promote_discord_action_escalation = AsyncMock(return_value="thread-url")
    source = _source(canonical)
    event = MessageEvent(
        text="Do not implement; plan only.",
        source=source,
        message_id="message-1",
        discord_runtime_mode="read_only",
        discord_action_escalation_allowed=False,
        discord_runtime_reason="explicit_no_implementation",
    )

    response = await runner._handle_message_with_agent(
        event,
        source,
        "agent:main:discord:thread:thread-123",
        1,
    )

    assert captured["escalation_allowed"] is True
    assert response is None
    runner._promote_discord_action_escalation.assert_awaited_once()
    runner.session_store.append_to_transcript.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("fable_implementation", [False, True])
@pytest.mark.parametrize("discord_action_request_intent", [None, False])
async def test_action_turn_injects_worktree_as_project_path_and_agent_cwd(
    tmp_path,
    monkeypatch,
    fable_implementation,
    discord_action_request_intent,
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
    event.discord_runtime_mode = "action"
    event.discord_action_request_intent = discord_action_request_intent
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
    assert (
        run_kwargs["discord_action_request_intent"]
        is discord_action_request_intent
    )
    assert run_kwargs["discord_runtime_mode"] == "action"
    assert str(canonical) not in run_kwargs["context_prompt"]
    assert f"Path: `{worktree_cwd}`" in run_kwargs["context_prompt"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "initial_request",
    [
        "Implement and ship this through the normal lifecycle.",
        "Implement this and open a PR only; do not merge it.",
    ],
)
@pytest.mark.parametrize("fable_implementation", [False, True])
async def test_direct_closeout_policy_always_publishes_without_merging(
    tmp_path,
    monkeypatch,
    initial_request,
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
    runner._persist_action_closeout_workspace = lambda _event, **kwargs: captured.update(
        closeout=kwargs
    )
    source = _source(canonical)
    event = MessageEvent(
        text="Proceed with the accepted request.",
        source=source,
        message_id="message-1",
    )
    event.feature_summary = {"initial_request": initial_request}
    event.discord_runtime_mode = "action"
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
    assert captured["closeout"]["source"] == (
        "fable" if fable_implementation else "direct"
    )
    assert captured["closeout"]["mode"] == "shadow"
    assert captured["closeout"]["policy"]["merge"] == "never"
    assert captured["closeout"]["policy"]["pr_open"] == "after_review_approval"


@pytest.mark.asyncio
async def test_fable_action_fallback_honors_legacy_closeout_opt_out(
    tmp_path,
    monkeypatch,
):
    canonical_root = tmp_path / "canonical"
    canonical = canonical_root / "PID"
    workspaces = tmp_path / "workspaces"
    _init_repo(canonical)
    _protect(monkeypatch, canonical_root)
    config = _config(canonical)
    config["closeout"] = {
        "mode": "enforce",
        "surfaces": {"direct": True},
    }
    config["fable"] = {"git_lifecycle": "none"}
    monkeypatch.setattr(gateway_run, "_DISCORD_ACTION_WORKTREE_ROOT", workspaces)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: config)
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
    runner._persist_action_closeout_workspace = lambda _event, **kwargs: captured.update(
        closeout=kwargs
    )
    event = MessageEvent(
        text="Proceed with Fable.",
        source=_source(canonical),
        message_id="message-1",
    )
    event.feature_summary = _feature_summary()
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
        event.source,
        "agent:main:discord:thread:thread-123",
        1,
    )

    assert response == "done"
    assert captured["closeout"]["source"] == "fable"
    assert captured["closeout"]["mode"] == "off"
