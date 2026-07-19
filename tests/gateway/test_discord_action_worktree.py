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
            "surfaces": {"direct": False, "fable": True},
            "early_draft_pr": False,
            "post_merge_requirements": {"ci": False, "restart": False},
            "repositories": {
                "acme/pid": {
                    "mode": "enforce",
                    "surfaces": {"direct": True},
                    "early_draft_pr": True,
                    "visual_qa": {"mode": "enforce_explicit"},
                    "post_merge_requirements": {"ci": True},
                }
            },
        },
        "acme/pid",
    )

    assert effective["mode"] == "enforce"
    assert effective["surfaces"] == {"direct": True, "fable": True}
    assert effective["early_draft_pr"] is True
    assert effective["visual_qa"] == {"mode": "enforce_explicit"}
    assert effective["post_merge_requirements"] == {
        "ci": True,
        "restart": False,
    }


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
        assert calls == [(expected_command, tmp_path, 180.0)]
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
        assert timeout == 0.25
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
        assert timeout == 180.0
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
):
    captured = {}
    item = {
        "id": "work-1",
        "closeout_authoritative": False,
        "closeout": {
            "revision": 4,
            "source": "direct",
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
        def get(self, work_id):
            return item if work_id == item["id"] else None

        def activate_closeout(self, work_id, state, *, expected_revision):
            captured.update(
                work_id=work_id,
                state=state,
                expected_revision=expected_revision,
            )
            return {**state, "revision": expected_revision + 1}

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
def test_direct_closeout_activates_clean_verified_exact_head(
    tmp_path,
    monkeypatch,
    mode,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    canonical = tmp_path / "canonical"
    mutable = tmp_path / "mutable"
    _init_repo(canonical)
    _run(canonical, "worktree", "add", "-b", "discord/action-test", str(mutable), "HEAD")
    runner, captured, notifications = _direct_closeout_runner(mutable, mode=mode)
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
async def test_successful_promotion_stays_out_of_current_turn_until_target_inbound(
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
    promoted_summaries: dict[str, dict] = {}
    callbacks = []
    adapter = SimpleNamespace(
        _active_sessions={},
        _load_feature_summary_handle_by_thread_id=lambda thread_id: promoted_summaries.get(thread_id),
        register_post_delivery_callback=lambda *args, **kwargs: callbacks.append((args, kwargs)),
        send=AsyncMock(),
    )
    runner.adapters = {Platform.DISCORD: adapter}
    runner._session_has_pending_background_workers = lambda *args, **kwargs: False
    runner._discord_ledger_summary_status = lambda _work_id, status: status
    runner._register_discord_summary_post_delivery = (
        gateway_run.GatewayRunner._register_discord_summary_post_delivery.__get__(runner)
    )
    source = _source(canonical)
    event = MessageEvent(
        text="Can you explain this first?",
        source=source,
        message_id="message-1",
        discord_action_request_intent=False,
    )
    promotion_link = "https://discord.com/channels/guild-1/thread-123"

    async def _promote_then_stop(**_kwargs):
        promoted_summaries["thread-123"] = feature_summary
        return {
            "final_response": (
                f"I created the action thread: {promotion_link}. "
                "Please continue by sending a new message there."
            ),
            "messages": [
                {"role": "user", "content": event.text},
                {"role": "assistant", "content": promotion_link},
            ],
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 0,
        }

    runner._run_agent = AsyncMock(side_effect=_promote_then_stop)

    response = await runner._handle_message_with_agent(
        event,
        source,
        "agent:main:discord:thread:thread-123",
        1,
    )

    assert promotion_link in response
    assert "sending a new message" in response
    assert event.source is source
    assert event.feature_summary is None
    assert callbacks == []

    next_event = MessageEvent(
        text="Build it now.",
        source=source,
        message_id="message-2",
    )
    assert runner._hydrate_discord_feature_summary_from_adapter(next_event) == feature_summary
    assert next_event.feature_summary == feature_summary
    assert gateway_run._is_standard_discord_action_request(
        next_event.source,
        next_event.feature_summary,
    )
    tier = gateway_run._discord_action_request_model_tier({}, next_event.feature_summary)
    assert tier.name == "discord_action"
    assert tier.reasoning_effort == "medium"


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
    assert str(canonical) not in run_kwargs["context_prompt"]
    assert f"Path: `{worktree_cwd}`" in run_kwargs["context_prompt"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("initial_request", "expected_merge"),
    [
        ("Implement and ship this through the normal lifecycle.", "auto"),
        ("Implement this and open a PR only; do not merge it.", "never"),
    ],
)
async def test_direct_closeout_policy_preserves_pr_lifecycle_intent(
    tmp_path,
    monkeypatch,
    initial_request,
    expected_merge,
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

    response = await runner._handle_message_with_agent(
        event,
        source,
        "agent:main:discord:thread:thread-123",
        1,
    )

    assert response == "done"
    assert captured["closeout"]["source"] == "direct"
    assert captured["closeout"]["policy"]["merge"] == expected_merge
    assert captured["closeout"]["policy"]["pr_open"] == "after_review_approval"
