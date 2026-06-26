from __future__ import annotations

import json
import inspect
import os
import sqlite3
import stat
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


def _home(monkeypatch, tmp_path: Path) -> Path:
    root = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_CODEX_WORKER_RUNNER", raising=False)
    monkeypatch.delenv("HERMES_CODING_WORKER_BACKEND", raising=False)
    monkeypatch.delenv("HERMES_CODEX_WORKER_REASONING", raising=False)
    monkeypatch.delenv("HERMES_CODEX_WORKER_SERVICE_TIER", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_WORKER_SYSTEMD", "0")
    return root


def _pr_view_json(
    *,
    number: int = 123,
    state: str = "MERGED",
    merge_state: str = "CLEAN",
    mergeable: str = "MERGEABLE",
    checks: list[dict] | None = None,
) -> str:
    if checks is None:
        checks = [{"name": "unit", "status": "COMPLETED", "conclusion": "SUCCESS"}]
    return json.dumps(
        {
            "number": number,
            "url": f"https://github.com/sligo-labs/PID/pull/{number}",
            "state": state,
            "mergedAt": "2026-05-26T15:30:17Z" if state == "MERGED" else None,
            "mergeCommit": {"oid": "abc123"} if state == "MERGED" else None,
            "mergeStateStatus": merge_state,
            "mergeable": mergeable,
            "isDraft": False,
            "reviewDecision": "",
            "statusCheckRollup": checks,
        }
    )


def _canonical_sync_result(
    cmd: list[str],
    *,
    head: str = "def456",
    dirty: bool = False,
    pull_failed: bool = False,
    ancestor_failed: bool = False,
) -> SimpleNamespace | None:
    if cmd == ["git", "status", "--porcelain"]:
        return SimpleNamespace(returncode=0, stdout=" M file.py\n" if dirty else "", stderr="")
    if cmd == ["git", "fetch", "origin", "--prune"]:
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    if cmd[:2] == ["git", "checkout"]:
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    if cmd[:4] == ["git", "pull", "--ff-only", "origin"]:
        if pull_failed:
            return SimpleNamespace(returncode=1, stdout="", stderr="not fast-forward")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    if cmd == ["git", "rev-parse", "HEAD"]:
        return SimpleNamespace(returncode=0, stdout=f"{head}\n", stderr="")
    if cmd[:3] == ["git", "merge-base", "--is-ancestor"]:
        if ancestor_failed:
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    return None


def _claimed_planner(monkeypatch, tmp_path: Path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db

    board = dwb.set_goal(thread_id="9001", goal="Plan with Codex")
    conn = kanban_db.connect(board=board.slug)
    try:
        task = kanban_db.list_tasks(conn, include_archived=False)[0]
        claimed = kanban_db.claim_task(conn, task.id)
    finally:
        conn.close()
    assert claimed is not None
    return board, claimed


def test_check_rollup_summary_uses_latest_duplicate_check_run():
    from hermes_cli import kanban_codex_worker as worker

    status, total, failed = worker._check_rollup_summary(
        [
            {
                "name": "basic",
                "workflowName": "Basic Tests",
                "status": "COMPLETED",
                "conclusion": "CANCELLED",
                "startedAt": "2026-06-09T15:00:00Z",
            },
            {
                "name": "supply-chain",
                "workflowName": "Supply Chain Audit",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
                "startedAt": "2026-06-09T15:01:00Z",
            },
            {
                "name": "basic",
                "workflowName": "Basic Tests",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
                "startedAt": "2026-06-09T15:02:00Z",
            },
        ]
    )

    assert status == "passed"
    assert total == 2
    assert failed == []


def test_worker_retry_operation_names_are_backend_neutral():
    from hermes_cli import kanban_codex_worker as worker

    source = inspect.getsource(worker)

    assert 'operation_name="coding_worker.initial_get_task"' in source
    assert 'operation_name="coding_worker.recovery_get_task"' in source
    assert 'operation_name="coding_worker.recovery_confirm_get_task"' in source
    assert 'operation_name="coding_worker.recorded_result_get_task"' in source
    assert 'operation_name="codex_worker.' not in source


def test_backend_child_env_bridges_profile_cli_paths(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_worker as worker

    hermes_home = tmp_path / "hermes-home"
    profile_home = hermes_home / "home"
    profile_local_bin = profile_home / ".local" / "bin"
    profile_foundry_bin = profile_home / ".foundry" / "bin"
    profile_cargo_bin = profile_home / ".cargo" / "bin"
    profile_local_bin.mkdir(parents=True)
    profile_foundry_bin.mkdir(parents=True)
    profile_cargo_bin.mkdir(parents=True)

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HOME", "/home/operator")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-control-state")

    env = worker._backend_child_env({"HERMES_KANBAN_WORKSPACE": "/workspace"})

    assert env["HOME"] == str(profile_home)
    assert env["HERMES_HOME"] == str(hermes_home)
    assert "HERMES_KANBAN_TASK" not in env
    assert env["HERMES_KANBAN_WORKSPACE"] == "/workspace"
    assert env["PATH"].split(os.pathsep)[:3] == [
        str(profile_local_bin),
        str(profile_foundry_bin),
        str(profile_cargo_bin),
    ]


def test_backend_child_env_respects_explicit_path(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_worker as worker

    hermes_home = tmp_path / "hermes-home"
    (hermes_home / "home" / ".foundry" / "bin").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    env = worker._backend_child_env({"PATH": "/explicit/bin"})

    assert env["HOME"] == str(hermes_home / "home")
    assert env["PATH"] == "/explicit/bin"


def test_run_gh_bridges_real_gh_config_dir_when_home_is_isolated(monkeypatch, tmp_path):
    from hermes_cli import github_remote
    from hermes_cli import kanban_codex_worker as worker

    isolated_home = tmp_path / "hermes-home" / "home"
    real_gh_config = tmp_path / "real-home" / ".config" / "gh"
    real_gh_config.mkdir(parents=True)
    captured: dict[str, object] = {}

    monkeypatch.setenv("HOME", str(isolated_home))
    monkeypatch.setenv("GH_TOKEN", "gho_secret")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.delenv("GH_CONFIG_DIR", raising=False)
    monkeypatch.setattr(
        github_remote,
        "get_github_cli_config_dir",
        lambda env: str(real_gh_config) if env.get("HOME") == str(isolated_home) else "",
    )

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs.get("env")
        captured["cwd"] = kwargs.get("cwd")
        return subprocess.CompletedProcess(args, 0, stdout="{}", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)

    result = worker._run_gh(["auth", "status"], root=tmp_path, timeout=5)

    assert result.returncode == 0
    assert captured["args"] == ["gh", "auth", "status"]
    assert captured["cwd"] == tmp_path
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["HOME"] == str(isolated_home)
    assert env["GH_CONFIG_DIR"] == str(real_gh_config)
    assert "GH_TOKEN" not in env
    assert "GITHUB_TOKEN" not in env


def _write_codex_auth(path: Path, *, access: str, refresh: str, id_token: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "auth.json").write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {
                    "access_token": access,
                    "refresh_token": refresh,
                    "id_token": id_token,
                },
            }
        ),
        encoding="utf-8",
    )


def _write_pool_auth(hermes_home: Path, entries: list[dict]) -> None:
    hermes_home.mkdir(parents=True, exist_ok=True)
    (hermes_home / "auth.json").write_text(
        json.dumps(
            {
                "version": 1,
                "credential_pool": {"openai-codex": entries},
            }
        ),
        encoding="utf-8",
    )


def test_dev_role_prompt_includes_autoreview_closeout_contract(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_DEV, ROLE_PLANNER

    board = "discord-worker-autoreview-closeout"
    kanban_db.create_board(board, name="Autoreview closeout")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path))
    conn = kanban_db.connect(board=board)
    try:
        dev_task_id = kanban_db.create_task(
            conn,
            title="Implement parser fix",
            body="Goal: fix parser\nSuccess means: tests pass\nStop when: local verification recorded",
            assignee=ROLE_DEV,
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )
        planner_task_id = kanban_db.create_task(
            conn,
            title="Plan parser fix",
            assignee=ROLE_PLANNER,
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )
        dev_prompt = worker._build_prompt(conn, dev_task_id, ROLE_DEV)
        planner_prompt = worker._build_prompt(conn, planner_task_id, ROLE_PLANNER)
    finally:
        conn.close()

    assert "Autoreview closeout contract for dev workers" in dev_prompt
    assert ".agents/skills/autoreview/scripts/autoreview --mode local" in dev_prompt
    assert "Hermes materializes this repo-local helper" in dev_prompt
    assert "Record the autoreview command/result" in dev_prompt
    assert "Autoreview closeout contract for dev workers" not in planner_prompt


def test_dev_role_prompt_force_loads_board_worker_skill_hints(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import DISCORD_WORKER_META_KEY, ROLE_DEV, ROLE_PLANNER

    board = "discord-worker-solidity-skill"
    kanban_db.create_board(board, name="Solidity skill")
    meta = kanban_db.read_board_metadata(board)
    meta.pop("db_path", None)
    meta[DISCORD_WORKER_META_KEY] = {
        "kind": "discord_worker_board",
        "project_context": {"worker_skill_hints": ["reserve-solidity-style"]},
    }
    kanban_db.board_metadata_path(board).write_text(json.dumps(meta), encoding="utf-8")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(worker, "_git_summary", lambda _workspace: "clean")

    loaded: list[tuple[str, str]] = []

    def fake_render_skill(name: str, *, task_id: str) -> str:
        loaded.append((name, task_id))
        return f"LOADED-SKILL {name} for {task_id}"

    monkeypatch.setattr(worker, "_render_worker_skill", fake_render_skill)

    conn = kanban_db.connect(board=board)
    try:
        dev_task_id = kanban_db.create_task(
            conn,
            title="Implement Reserve contract amend",
            body="Goal: update Solidity PR\nSuccess means: follows Reserve style\nStop when: tests recorded",
            assignee=ROLE_DEV,
            workspace_kind="dir",
            workspace_path=str(tmp_path),
            tenant=board,
        )
        planner_task_id = kanban_db.create_task(
            conn,
            title="Plan Reserve amend",
            assignee=ROLE_PLANNER,
            workspace_kind="dir",
            workspace_path=str(tmp_path),
            tenant=board,
        )
        dev_prompt = worker._build_prompt(conn, dev_task_id, ROLE_DEV)
        planner_prompt = worker._build_prompt(conn, planner_task_id, ROLE_PLANNER)
    finally:
        conn.close()

    assert loaded == [("reserve-solidity-style", dev_task_id)]
    assert "Force-loaded implementation skills for this dev worker" in dev_prompt
    assert f"LOADED-SKILL reserve-solidity-style for {dev_task_id}" in dev_prompt
    assert "reserve-solidity-style" not in planner_prompt


def test_dev_role_prompt_force_loads_task_skills_and_deduplicates(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_DEV

    board = "discord-worker-task-skills"
    kanban_db.create_board(board, name="Task skills")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(worker, "_git_summary", lambda _workspace: "clean")
    monkeypatch.setattr(worker, "_render_worker_skill", lambda name, *, task_id: f"SKILL {name}")

    conn = kanban_db.connect(board=board)
    try:
        dev_task_id = kanban_db.create_task(
            conn,
            title="Implement skilled task",
            body="Goal: implement\nSuccess means: done\nStop when: verified",
            assignee=ROLE_DEV,
            workspace_kind="dir",
            workspace_path=str(tmp_path),
            tenant=board,
            skills=["general-coding", "reserve-solidity-style", "general-coding"],
        )
        dev_prompt = worker._build_prompt(conn, dev_task_id, ROLE_DEV)
    finally:
        conn.close()

    assert dev_prompt.count("SKILL general-coding") == 1
    assert dev_prompt.count("SKILL reserve-solidity-style") == 1


def test_autoreview_helper_materializes_in_hermes_and_pid_like_workspaces(tmp_path):
    from hermes_cli.worker_autoreview import AUTOREVIEW_RELATIVE_HELPER, AUTOREVIEW_RELATIVE_SKILL
    from hermes_cli.worker_autoreview import materialize_autoreview_helper

    for name in ("hermes-worker", "pid-worker"):
        workspace = tmp_path / name
        workspace.mkdir()

        helper = materialize_autoreview_helper(workspace)

        assert helper == workspace / AUTOREVIEW_RELATIVE_HELPER
        assert helper.exists()
        assert stat.S_IMODE(helper.stat().st_mode) & stat.S_IXUSR
        assert (workspace / AUTOREVIEW_RELATIVE_SKILL).read_text(encoding="utf-8").startswith("---\nname: autoreview")
        assert "advisory_not_model_review" in helper.read_text(encoding="utf-8")


def test_autoreview_helper_is_excluded_from_git_status(tmp_path):
    from hermes_cli.worker_autoreview import materialize_autoreview_helper

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True, text=True)

    materialize_autoreview_helper(tmp_path)

    status = subprocess.run(
        ["git", "status", "--short"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert ".agents/skills/autoreview" not in status


def test_autoreview_helper_preserves_existing_repo_helper(tmp_path):
    from hermes_cli.worker_autoreview import AUTOREVIEW_RELATIVE_HELPER, AUTOREVIEW_RELATIVE_SKILL
    from hermes_cli.worker_autoreview import materialize_autoreview_helper

    helper = tmp_path / AUTOREVIEW_RELATIVE_HELPER
    skill = tmp_path / AUTOREVIEW_RELATIVE_SKILL
    helper.parent.mkdir(parents=True)
    skill.parent.mkdir(parents=True, exist_ok=True)
    helper.write_text("custom helper", encoding="utf-8")
    skill.write_text("custom skill", encoding="utf-8")

    materialize_autoreview_helper(tmp_path)

    assert helper.read_text(encoding="utf-8") == "custom helper"
    assert skill.read_text(encoding="utf-8") == "custom skill"


def test_worker_pr_body_adds_project_state_justification_for_operational_changes():
    from hermes_cli import kanban_codex_worker as worker
    from scripts.check_pr_body_format import check_project_state_requirement

    body = worker._worker_pr_body(
        {"public_url": "https://discord/thread", "root_goal": "Fix worker PR hygiene"},
        board="discord-board",
        changed_files=["hermes_cli/kanban_codex_worker.py"],
    )

    assert "Board: https://discord/thread" in body
    assert "## Summary\n" in body
    assert "## Verification\n" in body
    assert "Goal:\nFix worker PR hygiene" not in body
    assert worker._WORKER_PROJECT_STATE_JUSTIFICATION in body
    ok, _message = check_project_state_requirement(body, ["hermes_cli/kanban_codex_worker.py"])
    assert ok is True


def test_worker_pr_body_skips_project_state_justification_when_state_changed():
    from hermes_cli import kanban_codex_worker as worker

    body = worker._worker_pr_body(
        {"root_goal": "Update project ledger"},
        board="discord-board",
        changed_files=["hermes_cli/kanban_codex_worker.py", "docs/project-state.md"],
    )

    assert worker._WORKER_PROJECT_STATE_JUSTIFICATION not in body


def test_changed_files_for_pr_body_includes_branch_and_worktree_changes(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_worker as worker

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd == ["git", "diff", "--name-only", "origin/main...HEAD"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="gateway/run.py\n", stderr="")
        if cmd == ["git", "diff", "--name-only", "--cached"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd == ["git", "diff", "--name-only"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="docs/project-state.md\ngateway/run.py\n", stderr="")
        if cmd == ["git", "ls-files", "--others", "--exclude-standard"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="hermes_cli/kanban.py\n", stderr="")
        raise AssertionError(cmd)

    monkeypatch.setattr(worker.subprocess, "run", fake_run)

    assert worker._changed_files_for_pr_body(tmp_path, base="main") == [
        "gateway/run.py",
        "docs/project-state.md",
        "hermes_cli/kanban.py",
    ]
    assert calls == [
        ["git", "diff", "--name-only", "origin/main...HEAD"],
        ["git", "diff", "--name-only", "--cached"],
        ["git", "diff", "--name-only"],
        ["git", "ls-files", "--others", "--exclude-standard"],
    ]


def test_ensure_existing_worker_pr_body_appends_project_state_justification(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_worker as worker

    calls = []
    worker_meta = {"pr_url": "https://github.com/sligo-labs/hermes-agent/pull/123", "root_goal": "Fix worker PR hygiene"}

    def fake_run_gh(args, *, root, timeout):
        calls.append(args)
        if args[:2] == ["pr", "view"]:
            return subprocess.CompletedProcess(args, 0, stdout="Board: board\n\nGoal:\nFix worker PR hygiene\n", stderr="")
        if args[:2] == ["pr", "edit"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        raise AssertionError(args)

    monkeypatch.setattr(worker, "_run_gh", fake_run_gh)

    worker._ensure_worker_pr_body_hygiene(
        worker_meta,
        root=tmp_path,
        repo="sligo-labs/hermes-agent",
        board="discord-board",
        changed_files=["hermes_cli/kanban_codex_worker.py"],
    )

    assert calls[0][:4] == ["pr", "view", worker_meta["pr_url"], "--repo"]
    assert calls[1][:4] == ["pr", "edit", worker_meta["pr_url"], "--repo"]
    assert calls[1][-1].endswith(worker._WORKER_PROJECT_STATE_JUSTIFICATION)
    assert "pr_body_update_error" not in worker_meta


def test_ensure_existing_worker_pr_body_does_not_edit_when_requirement_satisfied(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_worker as worker

    calls = []
    body = f"Board: board\n\nGoal:\nFix worker PR hygiene\n\n{worker._WORKER_PROJECT_STATE_JUSTIFICATION}"

    def fake_run_gh(args, *, root, timeout):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=body, stderr="")

    monkeypatch.setattr(worker, "_run_gh", fake_run_gh)

    worker._ensure_worker_pr_body_hygiene(
        {"pr_url": "https://github.com/sligo-labs/hermes-agent/pull/123"},
        root=tmp_path,
        repo="sligo-labs/hermes-agent",
        board="discord-board",
        changed_files=["hermes_cli/kanban_codex_worker.py"],
    )

    assert len(calls) == 1
    assert calls[0][:2] == ["pr", "view"]


def test_role_workers_materialize_autoreview_before_backend(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli.discord_worker_boards import ROLE_DEV, ROLE_PLANNER, ROLE_REVIEWER

    calls: list[Path] = []

    def fake_materialize(workspace):
        calls.append(Path(workspace))
        return Path(workspace) / ".agents/skills/autoreview/scripts/autoreview"

    monkeypatch.setattr(worker, "materialize_autoreview_helper", fake_materialize)
    monkeypatch.setattr(worker, "_role_uses_opencode", lambda role, task: False)
    monkeypatch.setattr(worker, "_run_codex", lambda *args, **kwargs: SimpleNamespace(final_text="{}", error=None))

    worker._run_role_backend("prompt", str(tmp_path), ROLE_DEV, task=SimpleNamespace(), task_id="t1", board="b1")
    worker._run_role_backend("prompt", str(tmp_path), ROLE_PLANNER, task=SimpleNamespace(), task_id="t2", board="b1")
    worker._run_role_backend("prompt", str(tmp_path), ROLE_REVIEWER, task=SimpleNamespace(), task_id="t3", board="b1")

    assert calls == [tmp_path, tmp_path, tmp_path]


def test_role_worker_reports_autoreview_materialization_failure_to_backend(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli.discord_worker_boards import ROLE_DEV

    prompts: list[str] = []

    monkeypatch.setattr(
        worker,
        "materialize_autoreview_helper",
        lambda _workspace: (_ for _ in ()).throw(RuntimeError("readonly workspace")),
    )
    monkeypatch.setattr(worker, "_role_uses_opencode", lambda role, task: False)
    monkeypatch.setattr(
        worker,
        "_run_codex",
        lambda prompt, *args, **kwargs: prompts.append(prompt) or SimpleNamespace(final_text="{}", error=None),
    )

    worker._run_role_backend("prompt", str(tmp_path), ROLE_DEV, task=SimpleNamespace(), task_id="t1", board="b1")

    assert "Autoreview helper materialization failed before dev worker start: readonly workspace" in prompts[0]


def test_coding_worker_activity_heartbeat_rate_limits_and_uses_run_id(monkeypatch):
    from hermes_cli import kanban_codex_worker as worker

    calls: list[tuple[str, int | None]] = []

    class Conn:
        def close(self):
            pass

    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "42")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t1")
    monkeypatch.setattr(worker.kanban_db, "connect", lambda board=None: Conn())
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "claimer-lock")
    monkeypatch.setattr(
        worker.kanban_db,
        "heartbeat_claim",
        lambda conn, task_id, claimer=None: calls.append(("claim", claimer)) or True,
    )
    monkeypatch.setattr(
        worker.kanban_db,
        "heartbeat_worker",
        lambda conn, task_id, note=None, expected_run_id=None: calls.append(("worker", expected_run_id)) or True,
    )
    monkeypatch.setattr(worker.time, "monotonic", lambda: 100.0)
    worker._last_activity_heartbeat_at.clear()

    worker._heartbeat_worker_activity("t1", board="b1")
    worker._heartbeat_worker_activity("t1", board="b1")

    assert calls == [("claim", "claimer-lock"), ("worker", 42)]

    worker._heartbeat_worker_activity("t1", board="b1", force=True)
    assert calls == [
        ("claim", "claimer-lock"),
        ("worker", 42),
        ("claim", "claimer-lock"),
        ("worker", 42),
    ]


def test_coding_worker_activity_heartbeat_is_best_effort(monkeypatch):
    from hermes_cli import kanban_codex_worker as worker

    worker._last_activity_heartbeat_at.clear()
    monkeypatch.setattr(worker.kanban_db, "connect", lambda board=None: (_ for _ in ()).throw(RuntimeError("db locked")))

    worker._heartbeat_worker_activity("t1", board="b1", force=True)


def test_role_completion_recovers_when_run_pointer_rotates_but_claim_is_owned(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db

    board = "discord-race"
    kanban_db.create_board(board, name="Race board")
    conn = kanban_db.connect(board=board)
    try:
        task_id = kanban_db.create_task(
            conn,
            title="Dev ticket",
            assignee="dev",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )
        claimed = kanban_db.claim_task(conn, task_id)
        assert claimed is not None
        original_run_id = claimed.current_run_id
        replacement = conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, claim_lock, "
            "claim_expires, started_at) VALUES (?, ?, 'running', ?, ?, ?)",
            (task_id, "dev", claimed.claim_lock, claimed.claim_expires, int(time.time())),
        )
        conn.execute(
            "UPDATE tasks SET current_run_id = ? WHERE id = ?",
            (replacement.lastrowid, task_id),
        )
        conn.commit()
        monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", claimed.claim_lock)

        completed = worker._complete_role_task(
            conn,
            task_id,
            summary="Implemented in checkpoint commit.",
            metadata={"raw": {"status": "completed"}},
            expected_run_id=original_run_id,
        )

        task = kanban_db.get_task(conn, task_id)
        latest = kanban_db.latest_run(conn, task_id)
    finally:
        conn.close()

    assert completed is True
    assert task is not None
    assert task.status == "done"
    assert latest is not None
    assert latest.outcome == "completed"
    assert latest.summary == "Implemented in checkpoint commit."


def test_role_completion_does_not_recover_when_claim_is_not_owned(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db

    board = "discord-race-unowned"
    kanban_db.create_board(board, name="Race board")
    conn = kanban_db.connect(board=board)
    try:
        task_id = kanban_db.create_task(conn, title="Dev ticket", assignee="dev")
        claimed = kanban_db.claim_task(conn, task_id)
        assert claimed is not None
        conn.execute(
            "UPDATE tasks SET current_run_id = current_run_id + 1 WHERE id = ?",
            (task_id,),
        )
        conn.commit()
        monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "someone-else")

        completed = worker._complete_role_task(
            conn,
            task_id,
            summary="Should not complete.",
            metadata={"raw": {"status": "completed"}},
            expected_run_id=claimed.current_run_id,
        )

        task = kanban_db.get_task(conn, task_id)
    finally:
        conn.close()

    assert completed is False
    assert task is not None
    assert task.status == "running"


def test_role_worker_exits_zero_after_recording_backend_blocker(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_REVIEWER

    board = "discord-worker-error"
    kanban_db.create_board(board, name="Worker error board")
    conn = kanban_db.connect(board=board)
    try:
        task_id = kanban_db.create_task(conn, title="Review", assignee=ROLE_REVIEWER)
        claimed = kanban_db.claim_task(conn, task_id)
        assert claimed is not None
    finally:
        conn.close()

    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", board)
    monkeypatch.setenv("HERMES_CODEX_WORKER_ROLE", ROLE_REVIEWER)
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(worker, "_build_prompt", lambda _conn, _task_id, _role: "prompt")
    monkeypatch.setattr(
        worker,
        "_run_role_backend",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("backend exploded")),
    )
    monkeypatch.setattr(worker, "mark_dispatch_dirty", lambda **_kwargs: None)

    assert worker.main() == 0

    conn = kanban_db.connect(board=board)
    try:
        task = kanban_db.get_task(conn, task_id)
        latest = kanban_db.latest_run(conn, task_id)
    finally:
        conn.close()
    assert task is not None
    assert task.status == "blocked"
    assert latest is not None
    assert latest.outcome == "blocked"
    assert "backend exploded" in (latest.summary or "")


def test_role_worker_recovers_completed_json_after_transient_apply_failure(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_REVIEWER

    board = "discord-worker-recover-result"
    kanban_db.create_board(board, name="Worker recovery board")
    conn = kanban_db.connect(board=board)
    try:
        task_id = kanban_db.create_task(conn, title="Review", assignee=ROLE_REVIEWER)
        claimed = kanban_db.claim_task(conn, task_id)
        assert claimed is not None
    finally:
        conn.close()

    payload = {
        "status": "blocked",
        "summary": "Reviewer could not finish.",
        "findings": [],
        "new_tasks": [],
        "criteria_assessment": {},
        "blocker": "Need operator input.",
    }
    real_apply = worker._apply_role_output
    calls = {"count": 0}

    def flaky_apply(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("transient sqlite lock after result")
        return real_apply(*args, **kwargs)

    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", board)
    monkeypatch.setenv("HERMES_CODEX_WORKER_ROLE", ROLE_REVIEWER)
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(worker, "_build_prompt", lambda _conn, _task_id, _role: "prompt")
    monkeypatch.setattr(
        worker,
        "_run_role_backend",
        lambda *args, **kwargs: SimpleNamespace(final_text=json.dumps(payload), error=None),
    )
    monkeypatch.setattr(worker, "_apply_role_output", flaky_apply)
    monkeypatch.setattr(worker, "mark_dispatch_dirty", lambda **_kwargs: None)

    assert worker.main() == 0
    assert calls["count"] == 2

    conn = kanban_db.connect(board=board)
    try:
        task = kanban_db.get_task(conn, task_id)
        latest = kanban_db.latest_run(conn, task_id)
    finally:
        conn.close()
    assert task is not None
    assert task.status == "blocked"
    assert latest is not None
    assert latest.outcome == "blocked"
    assert latest.summary == "Need operator input."


def test_role_worker_recovers_completed_json_with_fresh_connection_after_poisoned_conn(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_DEV

    board = "discord-worker-fresh-recover-result"
    kanban_db.create_board(board, name="Fresh recovery board")
    conn = kanban_db.connect(board=board)
    try:
        task_id = kanban_db.create_task(
            conn,
            title="Dev",
            assignee=ROLE_DEV,
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )
        claimed = kanban_db.claim_task(conn, task_id)
        assert claimed is not None
    finally:
        conn.close()

    payload = {
        "status": "completed",
        "summary": "Dev finished with valid JSON.",
        "changed_files": ["hermes_cli/kanban_codex_worker.py"],
        "tests": [
            {
                "command": "scripts/run_tests.sh tests/hermes_cli/test_kanban_codex_workers.py",
                "result": "passed",
                "output": "ok",
            }
        ],
        "handoff": {
            "changed_files": ["hermes_cli/kanban_codex_worker.py"],
            "tests": [],
            "verification": [],
            "preview": {"url": "", "command": "", "status": "not_run"},
            "smoke_routes": [],
            "known_warnings": [],
            "notes": "",
        },
        "blocker": None,
        "pr_ready": False,
    }
    real_apply = worker._apply_role_output
    calls = {"count": 0}

    def poison_first_connection(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            args[0].close()
            raise RuntimeError("sqlite connection died after model result")
        return real_apply(*args, **kwargs)

    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", board)
    monkeypatch.setenv("HERMES_CODEX_WORKER_ROLE", ROLE_DEV)
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(worker, "_build_prompt", lambda _conn, _task_id, _role: "prompt")
    monkeypatch.setattr(
        worker,
        "_run_role_backend",
        lambda *args, **kwargs: SimpleNamespace(final_text=json.dumps(payload), error=None),
    )
    monkeypatch.setattr(worker, "_apply_role_output", poison_first_connection)
    monkeypatch.setattr(worker, "_checkpoint_commit", lambda *args, **kwargs: None)
    monkeypatch.setattr(worker, "mark_dispatch_dirty", lambda **_kwargs: None)

    assert worker.main() == 0
    assert calls["count"] == 2

    conn = kanban_db.connect(board=board)
    try:
        task = kanban_db.get_task(conn, task_id)
        latest = kanban_db.latest_run(conn, task_id)
    finally:
        conn.close()
    assert task is not None
    assert task.status == "done"
    assert latest is not None
    assert latest.outcome == "completed"
    assert latest.summary == "Dev finished with valid JSON."


def test_role_worker_closes_poisoned_conn_before_fresh_result_recovery(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_REVIEWER

    board = "discord-worker-close-poisoned-recover-result"
    kanban_db.create_board(board, name="Close poisoned recovery board")
    conn = kanban_db.connect(board=board)
    try:
        task_id = kanban_db.create_task(conn, title="Review", assignee=ROLE_REVIEWER)
        claimed = kanban_db.claim_task(conn, task_id)
        assert claimed is not None
    finally:
        conn.close()

    payload = {
        "status": "blocked",
        "summary": "Reviewer reached a valid blocker.",
        "findings": [],
        "new_tasks": [],
        "criteria_assessment": {},
        "blocker": "Authenticated protected-route rendering requires a valid Agora session.",
    }
    real_connect = kanban_db.connect
    real_get_task_with_retry = kanban_db.get_task_with_transient_retry
    real_apply = worker._apply_role_output
    first_conn = {"proxy": None}
    calls = {"apply": 0, "fresh_before_close": 0}

    class ConnProxy:
        def __init__(self, wrapped):
            self.wrapped = wrapped
            self.closed = False

        def close(self):
            self.closed = True
            return self.wrapped.close()

        def __getattr__(self, name):
            return getattr(self.wrapped, name)

    def connect_with_poison_guard(*args, **kwargs):
        raw = real_connect(*args, **kwargs)
        if first_conn["proxy"] is None:
            first_conn["proxy"] = ConnProxy(raw)
            return first_conn["proxy"]
        if not first_conn["proxy"].closed:
            calls["fresh_before_close"] += 1
            raw.close()
            raise sqlite3.OperationalError("disk I/O error")
        return raw

    def get_task_with_poisoned_original(conn, *args, **kwargs):
        if conn is first_conn["proxy"] and kwargs.get("operation_name") == "coding_worker.recovery_get_task":
            raise sqlite3.OperationalError("disk I/O error")
        return real_get_task_with_retry(conn, *args, **kwargs)

    def fail_first_apply(*args, **kwargs):
        calls["apply"] += 1
        if calls["apply"] == 1:
            raise sqlite3.OperationalError("disk I/O error")
        return real_apply(*args, **kwargs)

    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", board)
    monkeypatch.setenv("HERMES_CODEX_WORKER_ROLE", ROLE_REVIEWER)
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(worker, "_build_prompt", lambda _conn, _task_id, _role: "prompt")
    monkeypatch.setattr(
        worker,
        "_run_role_backend",
        lambda *args, **kwargs: SimpleNamespace(final_text=json.dumps(payload), error=None),
    )
    monkeypatch.setattr(kanban_db, "connect", connect_with_poison_guard)
    monkeypatch.setattr(kanban_db, "get_task_with_transient_retry", get_task_with_poisoned_original)
    monkeypatch.setattr(worker, "_apply_role_output", fail_first_apply)
    monkeypatch.setattr(worker, "mark_dispatch_dirty", lambda **_kwargs: None)

    assert worker.main() == 0
    assert calls == {"apply": 2, "fresh_before_close": 0}
    assert first_conn["proxy"] is not None
    assert first_conn["proxy"].closed is True

    conn = real_connect(board=board)
    try:
        task = kanban_db.get_task(conn, task_id)
        latest = kanban_db.latest_run(conn, task_id)
    finally:
        conn.close()
    assert task is not None
    assert task.status == "blocked"
    assert latest is not None
    assert latest.outcome == "blocked"
    assert latest.summary == "Authenticated protected-route rendering requires a valid Agora session."


def test_role_worker_recovers_recorded_json_when_backend_raises_after_recording(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_DEV

    board = "discord-worker-recorded-recover-result"
    kanban_db.create_board(board, name="Recorded recovery board")
    conn = kanban_db.connect(board=board)
    try:
        task_id = kanban_db.create_task(
            conn,
            title="Dev",
            assignee=ROLE_DEV,
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )
        claimed = kanban_db.claim_task(conn, task_id)
        assert claimed is not None
    finally:
        conn.close()

    payload = {
        "status": "completed",
        "summary": "Dev result was recorded before cleanup failed.",
        "changed_files": ["hermes_cli/kanban_codex_worker.py"],
        "tests": [],
        "handoff": {
            "changed_files": ["hermes_cli/kanban_codex_worker.py"],
            "tests": [],
            "verification": [],
            "preview": {"url": "", "command": "", "status": "not_run"},
            "smoke_routes": [],
            "known_warnings": [],
            "notes": "",
        },
        "blocker": None,
        "pr_ready": False,
    }

    def backend_records_then_raises(*args, **kwargs):
        result = SimpleNamespace(
            final_text=json.dumps(payload),
            error=None,
            backend="opencode",
            exit_code=0,
        )
        worker.record_codex_worker_result(task_id, board=board, result=result)
        raise RuntimeError("post-result cleanup failed")

    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", board)
    monkeypatch.setenv("HERMES_CODEX_WORKER_ROLE", ROLE_DEV)
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(worker, "_build_prompt", lambda _conn, _task_id, _role: "prompt")
    monkeypatch.setattr(worker, "_run_role_backend", backend_records_then_raises)
    monkeypatch.setattr(worker, "_checkpoint_commit", lambda *args, **kwargs: None)
    monkeypatch.setattr(worker, "mark_dispatch_dirty", lambda **_kwargs: None)

    assert worker.main() == 0

    conn = kanban_db.connect(board=board)
    try:
        task = kanban_db.get_task(conn, task_id)
        latest = kanban_db.latest_run(conn, task_id)
    finally:
        conn.close()
    assert task is not None
    assert task.status == "done"
    assert latest is not None
    assert latest.outcome == "completed"
    assert latest.summary == "Dev result was recorded before cleanup failed."


def test_codex_role_worker_defaults_to_host_runner(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_workers as workers
    from hermes_cli import discord_worker_read

    board, task = _claimed_planner(monkeypatch, tmp_path)
    workspace = tmp_path / "repo"
    real_home = tmp_path / "real-home"
    gh_dir = real_home / ".config" / "gh"
    gh_dir.mkdir(parents=True)
    (gh_dir / "hosts.yml").write_text("github.com:\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(real_home))
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "discord-token")
    monkeypatch.setenv("DISCORD_ADMIN_ACTIONS", "delete,pin")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "parent-codex-home"))
    monkeypatch.setenv("HERMES_CODEX_WORKER_CREDENTIAL_ID", "parent-cred")
    monkeypatch.delenv("GH_CONFIG_DIR", raising=False)
    monkeypatch.setattr(
        discord_worker_read,
        "start_read_broker",
        lambda token: ("http://127.0.0.1:9", "broker-secret"),
    )
    captured = {}

    class Proc:
        pid = 4321

        def poll(self):
            return None

    def fake_popen(cmd, cwd, stdout, stderr, env, start_new_session):
        captured.update(
            {
                "cmd": cmd,
                "cwd": cwd,
                "env": env,
                "start_new_session": start_new_session,
            }
        )
        stdout.write(b"host worker launched\n")
        stdout.flush()
        return Proc()

    monkeypatch.setattr(
        workers,
        "_worker_config",
        lambda: {"backend": "codex", "codex_home_root": str(tmp_path / "homes")},
    )
    monkeypatch.setattr(workers.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(workers.subprocess, "Popen", fake_popen)

    pid = workers.spawn_codex_worker(task, str(workspace), board=board.slug)

    assert pid == 4321
    assert captured["cmd"] == workers._host_worker_cmd()
    assert captured["cwd"] == str(workspace.resolve())
    assert captured["env"]["HERMES_CODEX_WORKER_ROLE"] == "planner"
    assert captured["env"]["HERMES_CODEX_WORKER_REASONING"] == "xhigh"
    assert captured["env"]["HERMES_CODEX_WORKER_SERVICE_TIER"] == "normal"
    assert captured["env"]["HERMES_KANBAN_BOARD"] == board.slug
    assert captured["env"]["HERMES_DISCORD_WORKER_READ_URL"] == "http://127.0.0.1:9"
    assert captured["env"]["HERMES_DISCORD_WORKER_READ_TOKEN"] == "broker-secret"
    assert captured["env"]["HERMES_DISCORD_WORKER_CONTROL_URL"] == "http://127.0.0.1:9"
    assert captured["env"]["HERMES_DISCORD_WORKER_CONTROL_TOKEN"] == "broker-secret"
    assert "DISCORD_BOT_TOKEN" not in captured["env"]
    assert "DISCORD_ADMIN_ACTIONS" not in captured["env"]
    assert captured["env"]["CODEX_HOME"].endswith("/homes/" + task.id)
    assert captured["env"]["CODEX_HOME"] != str(tmp_path / "parent-codex-home")
    assert captured["env"].get("HERMES_CODEX_WORKER_CREDENTIAL_ID") != "parent-cred"
    assert captured["env"]["GH_CONFIG_DIR"] == str(gh_dir)
    assert captured["start_new_session"] is True


def test_codex_role_worker_pythonpath_prefers_runtime_venv_owner(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_workers as workers

    board, task = _claimed_planner(monkeypatch, tmp_path)
    runtime_root = tmp_path / "canonical-hermes"
    project_worktree = tmp_path / "workspaces" / "hermes-discord-old-branch"
    (runtime_root / "hermes_cli").mkdir(parents=True)
    (project_worktree / "hermes_cli").mkdir(parents=True)
    python = runtime_root / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(project_worktree))
    captured = {}

    class Proc:
        pid = 4321

        def poll(self):
            return None

    def fake_popen(cmd, cwd, stdout, stderr, env, start_new_session):
        captured.update({"cmd": cmd, "cwd": cwd, "env": env})
        return Proc()

    monkeypatch.setattr(workers.sys, "executable", str(python))
    monkeypatch.setattr(
        workers,
        "_worker_config",
        lambda: {"backend": "codex", "codex_home_root": str(tmp_path / "homes")},
    )
    monkeypatch.setattr(workers, "_write_minimal_codex_home", lambda path: None)
    monkeypatch.setattr(workers.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(workers.subprocess, "Popen", fake_popen)

    pid = workers.spawn_codex_worker(task, str(project_worktree), board=board.slug)

    assert pid == 4321
    pythonpath = captured["env"]["PYTHONPATH"].split(os.pathsep)
    assert pythonpath[0] == str(runtime_root)
    assert str(project_worktree) in pythonpath[1:]
    assert captured["cwd"] == str(project_worktree.resolve())


def test_codex_role_worker_rejects_excluded_workspace(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_workers as workers

    board, task = _claimed_planner(monkeypatch, tmp_path)
    workspace = tmp_path / "workspaces" / "quarantined"
    monkeypatch.setattr(
        workers,
        "_worker_config",
        lambda: {"excluded_workspaces": [str(workspace)]},
    )

    with pytest.raises(RuntimeError, match="workspace is quarantined"):
        workers.spawn_codex_worker(task, str(workspace), board=board.slug)


def test_codex_role_worker_uses_systemd_worker_handle_when_enabled(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_workers as workers
    from hermes_cli import kanban_db

    board, task = _claimed_planner(monkeypatch, tmp_path)
    workspace = tmp_path / "repo"
    monkeypatch.setenv("HERMES_KANBAN_WORKER_SYSTEMD", "1")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "discord-token")
    captured = {}

    def fake_spawn_systemd_worker(*, cmd, workspace, env, log_path, unit_name):
        captured.update(
            {
                "cmd": cmd,
                "workspace": workspace,
                "env": env,
                "log_path": log_path,
                "unit_name": unit_name,
            }
        )
        return kanban_db._SpawnHandle(pid=2468, unit=f"{unit_name}.service")

    monkeypatch.setattr(
        workers,
        "_worker_config",
        lambda: {"backend": "codex", "codex_home_root": str(tmp_path / "homes")},
    )
    monkeypatch.setattr(
        workers,
        "_configure_discord_read_broker",
        lambda env: env.update(
            {
                "HERMES_DISCORD_WORKER_READ_URL": "http://127.0.0.1:9",
                "HERMES_DISCORD_WORKER_READ_TOKEN": "broker-secret",
                "HERMES_DISCORD_WORKER_CONTROL_URL": "http://127.0.0.1:9",
                "HERMES_DISCORD_WORKER_CONTROL_TOKEN": "broker-secret",
            }
        ),
    )
    monkeypatch.setattr(kanban_db, "_should_use_systemd_worker", lambda: True)
    monkeypatch.setattr(kanban_db, "_spawn_systemd_worker", fake_spawn_systemd_worker)
    monkeypatch.setattr(
        workers.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("direct Popen fallback should not run"),
    )

    handle = workers.spawn_codex_worker(task, str(workspace), board=board.slug)

    assert isinstance(handle, kanban_db._SpawnHandle)
    assert handle.pid == 2468
    assert handle.unit == f"{captured['unit_name']}.service"
    assert captured["cmd"] == workers._host_worker_cmd()
    assert captured["workspace"] == str(workspace.resolve())
    assert captured["env"]["HERMES_CODEX_WORKER_ROLE"] == "planner"
    assert captured["env"]["HERMES_CODING_WORKER_BACKEND"] == "codex"
    assert captured["env"]["HERMES_DISCORD_WORKER_READ_TOKEN"] == "broker-secret"
    assert captured["env"]["CODEX_HOME"].endswith("/homes/" + task.id)


def test_systemd_worker_env_keeps_role_worker_runtime_keys(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import kanban_db

    filtered = kanban_db._systemd_worker_env(
        {
            "HERMES_KANBAN_TASK": "task-1",
            "HERMES_CODEX_WORKER_ROLE": "planner",
            "HERMES_CODEX_WORKER_REASONING": "xhigh",
            "HERMES_CODEX_WORKER_SERVICE_TIER": "fast",
            "HERMES_CODEX_WORKER_CREDENTIAL_ID": "cred-1",
            "HERMES_CODING_WORKER_BACKEND": "opencode",
            "HERMES_DISCORD_WORKER_READ_URL": "http://127.0.0.1:9",
            "HERMES_DISCORD_WORKER_READ_TOKEN": "broker-secret",
            "HERMES_DISCORD_WORKER_CONTROL_URL": "http://127.0.0.1:9",
            "HERMES_DISCORD_WORKER_CONTROL_TOKEN": "broker-secret",
            "CODEX_HOME": str(tmp_path / "codex-home"),
            "DISCORD_BOT_TOKEN": "discord-token",
            "OPENAI_API_KEY": "openai-secret",
        }
    )

    assert filtered["HERMES_KANBAN_TASK"] == "task-1"
    assert filtered["HERMES_CODEX_WORKER_ROLE"] == "planner"
    assert filtered["HERMES_CODEX_WORKER_REASONING"] == "xhigh"
    assert filtered["HERMES_CODEX_WORKER_SERVICE_TIER"] == "fast"
    assert filtered["HERMES_CODEX_WORKER_CREDENTIAL_ID"] == "cred-1"
    assert filtered["HERMES_CODING_WORKER_BACKEND"] == "opencode"
    assert filtered["HERMES_DISCORD_WORKER_READ_TOKEN"] == "broker-secret"
    assert filtered["HERMES_DISCORD_WORKER_CONTROL_TOKEN"] == "broker-secret"
    assert filtered["CODEX_HOME"] == str(tmp_path / "codex-home")
    assert "DISCORD_BOT_TOKEN" not in filtered
    assert "OPENAI_API_KEY" not in filtered


def test_repo_root_falls_back_to_imported_checkout_outside_venv(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_workers as workers

    python = tmp_path / "python"
    python.write_text("", encoding="utf-8")
    monkeypatch.setattr(workers.sys, "executable", str(python))

    assert workers._repo_root() == Path(workers.__file__).resolve().parent.parent


def test_host_worker_cmd_uses_absolute_runtime_script(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_workers as workers

    runtime_root = tmp_path / "canonical-hermes"
    (runtime_root / "hermes_cli").mkdir(parents=True)
    python = runtime_root / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    monkeypatch.setattr(workers.sys, "executable", str(python))

    assert workers._host_worker_cmd() == [
        str(python),
        str(runtime_root / "hermes_cli" / "kanban_codex_worker.py"),
    ]


def test_codex_role_worker_falls_back_to_direct_spawn_when_systemd_launch_fails(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_workers as workers
    from hermes_cli import kanban_db

    board, task = _claimed_planner(monkeypatch, tmp_path)
    workspace = tmp_path / "repo"
    monkeypatch.setenv("HERMES_KANBAN_WORKER_SYSTEMD", "1")
    captured = {}

    class Proc:
        pid = 5432

        def poll(self):
            return None

    def fake_popen(cmd, cwd, stdout, stderr, env, start_new_session):
        captured.update(
            {"cmd": cmd, "cwd": cwd, "env": env, "start_new_session": start_new_session}
        )
        return Proc()

    monkeypatch.setattr(
        workers,
        "_worker_config",
        lambda: {"backend": "codex", "codex_home_root": str(tmp_path / "homes")},
    )
    monkeypatch.setattr(workers.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(kanban_db, "_should_use_systemd_worker", lambda: True)
    monkeypatch.setattr(
        kanban_db,
        "_spawn_systemd_worker",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("no user manager")),
    )
    monkeypatch.setattr(workers.subprocess, "Popen", fake_popen)

    pid = workers.spawn_codex_worker(task, str(workspace), board=board.slug)

    assert pid == 5432
    assert captured["cwd"] == str(workspace.resolve())
    assert captured["start_new_session"] is True
    log = kanban_db.read_worker_log(task.id, board=board.slug)
    assert log is not None
    assert "systemd-run role worker launch failed" in log
    assert "falling back to direct spawn: no user manager" in log


def test_codex_role_worker_inherits_available_pool_credential(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_workers as workers

    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    (hermes_home / "auth.json").write_text(
        json.dumps(
            {
                "version": 1,
                "credential_pool": {
                    "openai-codex": [
                        {
                            "id": "cred-1",
                            "label": "primary",
                            "auth_type": "oauth",
                            "priority": 0,
                            "source": "manual:device_code",
                            "access_token": "access-1",
                            "refresh_token": "refresh-1",
                            "id_token": "id-1",
                            "last_status": "exhausted",
                            "last_status_at": time.time(),
                            "last_error_code": 429,
                            "last_error_reset_at": time.time() + 5 * 3600,
                        },
                        {
                            "id": "cred-2",
                            "label": "secondary",
                            "auth_type": "oauth",
                            "priority": 1,
                            "source": "manual:device_code",
                            "access_token": "access-2",
                            "refresh_token": "refresh-2",
                            "id_token": "id-2",
                        },
                    ]
                },
            }
        )
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "parent-codex-home"))
    monkeypatch.setenv("HERMES_CODEX_WORKER_CREDENTIAL_ID", "parent-cred")
    board, task = _claimed_planner(monkeypatch, tmp_path)
    workspace = tmp_path / "repo"
    captured = {}

    class Proc:
        pid = 4321

        def poll(self):
            return None

    def fake_popen(cmd, cwd, stdout, stderr, env, start_new_session):
        captured.update({"env": env})
        return Proc()

    monkeypatch.setattr(
        workers,
        "_worker_config",
        lambda: {"backend": "codex", "codex_home_root": str(tmp_path / "homes")},
    )
    monkeypatch.setattr(workers.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(workers.subprocess, "Popen", fake_popen)

    workers.spawn_codex_worker(task, str(workspace), board=board.slug)

    codex_home = Path(captured["env"]["CODEX_HOME"])
    payload = json.loads((codex_home / "auth.json").read_text())
    assert captured["env"]["HERMES_CODEX_WORKER_CREDENTIAL_ID"] == "cred-2"
    assert captured["env"]["CODEX_HOME"] != str(tmp_path / "parent-codex-home")
    assert codex_home.is_symlink()
    assert payload["tokens"]["access_token"] == "access-2"
    assert payload["tokens"]["refresh_token"] == "refresh-2"


def test_codex_worker_refreshes_pool_credential_missing_id_token(monkeypatch, tmp_path):
    from agent import credential_pool
    from agent.codex_worker_auth import prepare_codex_worker_home

    hermes_home = tmp_path / "hermes-home"
    _write_pool_auth(
        hermes_home,
        [
            {
                "id": "cred-1",
                "label": "primary",
                "auth_type": "oauth",
                "priority": 0,
                "source": "manual:device_code",
                "access_token": "access-old",
                "refresh_token": "refresh-old",
            },
        ],
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    calls = []

    def fake_refresh(access_token, refresh_token):
        calls.append((access_token, refresh_token))
        return {
            "access_token": "access-new",
            "refresh_token": "refresh-new",
            "id_token": "id-new",
            "last_refresh": "now",
        }

    monkeypatch.setattr(credential_pool.auth_mod, "refresh_codex_oauth_pure", fake_refresh)

    codex_home = tmp_path / "worker-codex-home"
    credential_id = prepare_codex_worker_home(codex_home, allow_fallback=False)

    payload = json.loads((codex_home / "auth.json").read_text(encoding="utf-8"))
    entry = credential_pool.load_pool("openai-codex").entries()[0]
    assert credential_id == "cred-1"
    assert calls == [("access-old", "refresh-old")]
    assert payload["tokens"]["access_token"] == "access-new"
    assert payload["tokens"]["refresh_token"] == "refresh-new"
    assert payload["tokens"]["id_token"] == "id-new"
    assert entry.id_token == "id-new"


def test_cleanup_codex_worker_home_allows_child_of_explicit_root(monkeypatch, tmp_path):
    from agent.codex_worker_auth import cleanup_codex_worker_home

    root = tmp_path / "codex-worker-homes"
    child = root / "task-1"
    child.mkdir(parents=True)
    (child / "auth.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HERMES_CODEX_WORKER_CLEANUP_ROOT", str(root))

    cleanup_codex_worker_home(child)

    assert root.exists()
    assert not child.exists()


def test_codex_role_worker_does_not_copy_inherited_worker_codex_home(monkeypatch, tmp_path):
    from agent import credential_pool
    from hermes_cli import kanban_codex_workers as workers

    parent_codex_home = tmp_path / "parent-worker-codex-home"
    _write_codex_auth(
        parent_codex_home,
        access="parent-worker-access",
        refresh="parent-worker-refresh",
        id_token="parent-worker-id",
    )
    monkeypatch.setenv("HOME", str(tmp_path / "home-without-codex-auth"))
    monkeypatch.setenv("CODEX_HOME", str(parent_codex_home))
    monkeypatch.setenv("HERMES_CODEX_WORKER_CREDENTIAL_ID", "parent-worker-cred")
    monkeypatch.setattr(credential_pool, "load_pool", lambda provider: None)
    board, task = _claimed_planner(monkeypatch, tmp_path)
    captured = {}

    class Proc:
        pid = 4321

        def poll(self):
            return None

    def fake_popen(cmd, cwd, stdout, stderr, env, start_new_session):
        captured.update({"env": env})
        return Proc()

    monkeypatch.setattr(
        workers,
        "_worker_config",
        lambda: {"backend": "codex", "codex_home_root": str(tmp_path / "homes")},
    )
    monkeypatch.setattr(workers.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(workers.subprocess, "Popen", fake_popen)

    workers.spawn_codex_worker(task, str(tmp_path / "repo"), board=board.slug)

    codex_home = Path(captured["env"]["CODEX_HOME"])
    assert captured["env"].get("HERMES_CODEX_WORKER_CREDENTIAL_ID") != "parent-worker-cred"
    assert captured["env"]["CODEX_HOME"] != str(parent_codex_home)
    assert not (codex_home / "auth.json").exists()


def test_codex_role_worker_does_not_copy_external_codex_home(monkeypatch, tmp_path):
    from agent import credential_pool
    from hermes_cli import kanban_codex_workers as workers

    source_codex_home = tmp_path / "source-codex-home"
    _write_codex_auth(
        source_codex_home,
        access="source-access",
        refresh="source-refresh",
        id_token="source-id",
    )
    monkeypatch.setenv("CODEX_HOME", str(source_codex_home))
    monkeypatch.delenv("HERMES_CODEX_WORKER_CREDENTIAL_ID", raising=False)
    monkeypatch.setattr(credential_pool, "load_pool", lambda provider: None)
    board, task = _claimed_planner(monkeypatch, tmp_path)
    captured = {}

    class Proc:
        pid = 4321

        def poll(self):
            return None

    def fake_popen(cmd, cwd, stdout, stderr, env, start_new_session):
        captured.update({"env": env})
        return Proc()

    monkeypatch.setattr(
        workers,
        "_worker_config",
        lambda: {"backend": "codex", "codex_home_root": str(tmp_path / "homes")},
    )
    monkeypatch.setattr(workers.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(workers.subprocess, "Popen", fake_popen)

    workers.spawn_codex_worker(task, str(tmp_path / "repo"), board=board.slug)

    codex_home = Path(captured["env"]["CODEX_HOME"])
    assert captured["env"].get("HERMES_CODEX_WORKER_CREDENTIAL_ID") is None
    assert captured["env"]["CODEX_HOME"] != str(source_codex_home)
    assert not (codex_home / "auth.json").exists()


def test_codex_role_worker_logs_scheduled_runtime_settings(monkeypatch, tmp_path):
    from agent import opencode_worker as ow
    from hermes_cli import kanban_codex_workers as workers
    from hermes_cli import kanban_db

    board, task = _claimed_planner(monkeypatch, tmp_path)

    class Proc:
        pid = 4321

        def poll(self):
            return None

    monkeypatch.setattr(
        workers,
        "_worker_config",
        lambda: {
            "codex_home_root": str(tmp_path / "homes"),
            "roles": {"planner": {"reasoning": "xhigh", "service_tier": "fast"}},
        },
    )
    monkeypatch.setattr(ow, "check_opencode_binary", lambda: (True, "/bin/opencode"))
    monkeypatch.setattr(workers.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(workers.subprocess, "Popen", lambda *args, **kwargs: Proc())

    workers.spawn_codex_worker(task, str(tmp_path / "repo"), board=board.slug)

    log = kanban_db.read_worker_log(task.id, board=board.slug)
    assert log is not None
    assert "[kanban dispatcher] scheduled OpenCode role worker: role=planner reasoning=xhigh mode=fast" in log


def test_planner_worker_env_carries_effective_opencode_backend(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_workers as workers
    from hermes_cli import kanban_db
    from agent import opencode_worker as ow

    board, task = _claimed_planner(monkeypatch, tmp_path)
    workspace = tmp_path / "repo"
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "parent-codex-home"))
    monkeypatch.setenv("HERMES_CODEX_WORKER_CREDENTIAL_ID", "parent-cred")
    captured = {}

    class Proc:
        pid = 4321

        def poll(self):
            return None

    def fake_popen(cmd, cwd, stdout, stderr, env, start_new_session):
        captured.update({"env": env})
        return Proc()

    monkeypatch.setattr(
        workers,
        "_worker_config",
        lambda: {"backend": "opencode", "codex_home_root": str(tmp_path / "homes")},
    )
    monkeypatch.setattr(ow, "check_opencode_binary", lambda: (True, "/bin/opencode"))
    monkeypatch.setattr(workers.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(workers.subprocess, "Popen", fake_popen)

    workers.spawn_codex_worker(task, str(workspace), board=board.slug)

    assert captured["env"]["HERMES_CODING_WORKER_BACKEND"] == "opencode"
    assert "CODEX_HOME" not in captured["env"]
    assert "HERMES_CODEX_WORKER_CREDENTIAL_ID" not in captured["env"]
    log = kanban_db.read_worker_log(task.id, board=board.slug)
    assert log is not None
    assert "scheduled OpenCode role worker: role=planner reasoning=xhigh mode=normal" in log


def test_command_center_repair_foreman_uses_codex_with_codex_config(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_workers as workers
    from hermes_cli import kanban_db

    _home(monkeypatch, tmp_path)
    board = "repair-codex-board"
    kanban_db.create_board(board, name="Repair Codex Board")
    conn = kanban_db.connect(board=board)
    try:
        task_id = kanban_db.create_task(
            conn,
            title="Repair blocked board",
            assignee="foreman",
            created_by="command-center-repair",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )
        task = kanban_db.claim_task(conn, task_id)
    finally:
        conn.close()
    assert task is not None
    captured = {}

    class Proc:
        pid = 4321

        def poll(self):
            return None

    def fake_popen(cmd, cwd, stdout, stderr, env, start_new_session):
        captured.update({"env": env})
        return Proc()

    monkeypatch.setattr(
        workers,
        "_worker_config",
        lambda: {"backend": "codex", "codex_home_root": str(tmp_path / "homes")},
    )
    monkeypatch.setattr(workers, "_write_minimal_codex_home", lambda path: None)
    monkeypatch.setattr(workers.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(workers.subprocess, "Popen", fake_popen)

    workers.spawn_codex_worker(task, str(tmp_path), board=board)

    assert captured["env"]["HERMES_CODING_WORKER_BACKEND"] == "codex"
    assert captured["env"]["CODEX_HOME"].endswith(f"/homes/{task.id}")
    log = kanban_db.read_worker_log(task.id, board=board)
    assert log is not None
    assert "scheduled Codex role worker: role=foreman reasoning=xhigh mode=normal" in log


def test_foreman_worker_env_defaults_to_opencode(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_workers as workers
    from hermes_cli import kanban_db
    from agent import opencode_worker as ow

    _home(monkeypatch, tmp_path)
    board = "repair-opencode-board"
    kanban_db.create_board(board, name="Repair OpenCode Board")
    conn = kanban_db.connect(board=board)
    try:
        task_id = kanban_db.create_task(
            conn,
            title="Repair blocked board",
            assignee="foreman",
            created_by="command-center-repair",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )
        task = kanban_db.claim_task(conn, task_id)
    finally:
        conn.close()
    assert task is not None
    captured = {}

    class Proc:
        pid = 4321

        def poll(self):
            return None

    def fake_popen(cmd, cwd, stdout, stderr, env, start_new_session):
        captured.update({"env": env})
        return Proc()

    monkeypatch.setattr(workers, "_worker_config", lambda: {})
    monkeypatch.setattr(ow, "check_opencode_binary", lambda: (True, "/bin/opencode"))
    monkeypatch.setattr(workers.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(workers.subprocess, "Popen", fake_popen)

    workers.spawn_codex_worker(task, str(tmp_path), board=board)

    assert captured["env"]["HERMES_CODING_WORKER_BACKEND"] == "opencode"
    assert "CODEX_HOME" not in captured["env"]
    log = kanban_db.read_worker_log(task.id, board=board)
    assert log is not None
    assert "scheduled OpenCode role worker: role=foreman reasoning=xhigh mode=normal" in log


def test_role_extra_args_use_scheduled_runtime_env(monkeypatch):
    from hermes_cli import kanban_codex_worker as worker

    monkeypatch.setenv("HERMES_CODEX_WORKER_REASONING", "low")
    monkeypatch.setenv("HERMES_CODEX_WORKER_SERVICE_TIER", "fast")

    assert worker._role_extra_args("planner") == [
        "-c", 'model_reasoning_effort="low"',
        "-c", 'service_tier="fast"',
    ]


def test_dev_runtime_auto_uses_fast_medium_for_simple_task(monkeypatch):
    from hermes_cli import kanban_codex_workers as workers

    monkeypatch.delenv("HERMES_CODEX_WORKER_REASONING", raising=False)
    monkeypatch.delenv("HERMES_CODEX_WORKER_SERVICE_TIER", raising=False)
    task = SimpleNamespace(
        title="Fix typo",
        body="Correct a README typo",
        result=None,
        last_failure_error=None,
        consecutive_failures=0,
        created_by="planner",
    )

    settings = workers._role_runtime_settings("dev", {}, task)

    assert settings["reasoning"] == "medium"
    assert settings["reasoning_source"] == "adaptive"
    assert settings["service_tier"] == "fast"
    assert settings["service_tier_source"] == "adaptive"


def test_dev_runtime_auto_keeps_risky_and_retry_work_high(monkeypatch):
    from hermes_cli import kanban_codex_workers as workers

    monkeypatch.delenv("HERMES_CODEX_WORKER_REASONING", raising=False)
    monkeypatch.delenv("HERMES_CODEX_WORKER_SERVICE_TIER", raising=False)
    risky = SimpleNamespace(
        title="Fix auth migration performance regression",
        body="Production auth path is slow after schema migration",
        result=None,
        last_failure_error=None,
        consecutive_failures=0,
        created_by="planner",
    )
    retry = SimpleNamespace(
        title="Fix parser",
        body="Small parser fix",
        result=None,
        last_failure_error="previous worker crashed",
        consecutive_failures=1,
        created_by="planner",
    )

    risky_settings = workers._role_runtime_settings("dev", {}, risky)
    retry_settings = workers._role_runtime_settings("dev", {}, retry)

    assert risky_settings["reasoning"] == "high"
    assert risky_settings["service_tier"] == "normal"
    assert retry_settings["reasoning"] == "xhigh"
    assert retry_settings["service_tier"] == "normal"


def test_runtime_explicit_config_and_env_override_auto(monkeypatch):
    from hermes_cli import kanban_codex_workers as workers

    task = SimpleNamespace(title="Fix typo", body="", consecutive_failures=0)
    cfg = {
        "roles": {"dev": {"reasoning": "low", "service_tier": "normal"}},
        "service_tier": "auto",
    }

    settings = workers._role_runtime_settings("dev", cfg, task)
    assert settings["reasoning"] == "low"
    assert settings["service_tier"] == "normal"
    assert settings["reasoning_source"] == "explicit"
    assert settings["service_tier_source"] == "explicit"

    monkeypatch.setenv("HERMES_CODEX_WORKER_REASONING", "xhigh")
    monkeypatch.setenv("HERMES_CODEX_WORKER_SERVICE_TIER", "fast")

    settings = workers._role_runtime_settings("dev", cfg, task)
    assert settings["reasoning"] == "low"
    assert settings["service_tier"] == "normal"

    cfg = {
        "roles": {"dev": {"reasoning": "auto", "service_tier": "auto"}},
        "service_tier": "normal",
    }
    settings = workers._role_runtime_settings("dev", cfg, task)
    assert settings["reasoning"] == "xhigh"
    assert settings["service_tier"] == "fast"


def test_planner_and_reviewer_auto_remain_xhigh(monkeypatch):
    from hermes_cli import kanban_codex_workers as workers

    monkeypatch.delenv("HERMES_CODEX_WORKER_REASONING", raising=False)
    task = SimpleNamespace(title="Plan work", body="", consecutive_failures=0)

    assert workers._role_runtime_settings("planner", {}, task)["reasoning"] == "xhigh"
    assert workers._role_runtime_settings("reviewer", {}, task)["reasoning"] == "xhigh"


def test_dispatch_recovers_recorded_role_result_before_dead_pid_crash(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_REVIEWER
    from hermes_cli.discord_worker_state import record_codex_worker_result

    board = dwb.start_direct_goal(thread_id="recover-review", goal="Review completed result")
    conn = kanban_db.connect(board=board.slug)
    try:
        task_id = kanban_db.create_task(
            conn,
            title="Review",
            assignee=ROLE_REVIEWER,
            tenant=board.slug,
            workspace_kind="dir",
            workspace_path=str(tmp_path / "repo"),
        )
        claimed = kanban_db.claim_task(conn, task_id)
        assert claimed is not None
        conn.execute(
            "UPDATE tasks SET worker_pid = ? WHERE id = ?",
            (999_999_999, task_id),
        )
        conn.execute(
            "UPDATE task_runs SET worker_pid = ? WHERE id = ?",
            (999_999_999, claimed.current_run_id),
        )
        conn.commit()
        record_codex_worker_result(
            task_id,
            board=board.slug,
            result=SimpleNamespace(
                backend="codex",
                final_text=json.dumps(
                    {
                        "status": "approved",
                        "summary": "Reviewer approved.",
                        "findings": [],
                        "new_tasks": [],
                    }
                ),
                error=None,
            ),
        )
        monkeypatch.setattr(kanban_db, "_pid_alive", lambda _pid: False)
        monkeypatch.setattr(kanban_db, "_classify_worker_exit", lambda _pid: ("unknown", None))

        result = kanban_db.dispatch_once(conn, board=board.slug, max_spawn=0)
        task = kanban_db.get_task(conn, task_id)
        runs = kanban_db.list_runs(conn, task_id)
    finally:
        conn.close()

    assert result.crashed == []
    assert result.auto_blocked == []
    assert task is not None
    assert task.status == "done"
    assert task.consecutive_failures == 0
    assert task.last_failure_error is None
    assert runs[-1].outcome == "completed"
    assert runs[-1].summary == "Reviewer approved."


def test_dispatch_recovers_recorded_role_result_before_stale_reclaim(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_REVIEWER
    from hermes_cli.discord_worker_state import record_codex_worker_result

    board = dwb.start_direct_goal(thread_id="recover-stale-review", goal="Review completed stale result")
    now = int(time.time())
    conn = kanban_db.connect(board=board.slug)
    try:
        task_id = kanban_db.create_task(
            conn,
            title="Review",
            assignee=ROLE_REVIEWER,
            tenant=board.slug,
            workspace_kind="dir",
            workspace_path=str(tmp_path / "repo"),
        )
        claimed = kanban_db.claim_task(conn, task_id)
        assert claimed is not None
        conn.execute(
            """
            UPDATE tasks
               SET worker_pid = ?, last_heartbeat_at = ?, claim_expires = ?
             WHERE id = ?
            """,
            (999_999_999, now - 500, now + 3600, task_id),
        )
        conn.execute(
            """
            UPDATE task_runs
               SET worker_pid = ?, started_at = ?, last_heartbeat_at = ?
             WHERE id = ?
            """,
            (999_999_999, now - 500, now - 500, claimed.current_run_id),
        )
        conn.commit()
        record_codex_worker_result(
            task_id,
            board=board.slug,
            result=SimpleNamespace(
                backend="codex",
                final_text=json.dumps(
                    {
                        "status": "approved",
                        "summary": "Reviewer approved stale run.",
                        "findings": [],
                        "new_tasks": [],
                    }
                ),
                error=None,
            ),
        )
        terminations = []
        monkeypatch.setattr(
            kanban_db,
            "_terminate_reclaimed_worker",
            lambda *args, **kwargs: terminations.append(args) or {"pid_terminated": False},
        )
        monkeypatch.setattr(kanban_db, "_pid_alive", lambda _pid: False)
        monkeypatch.setattr(kanban_db, "_classify_worker_exit", lambda _pid: ("unknown", None))

        result = kanban_db.dispatch_once(
            conn,
            board=board.slug,
            max_spawn=0,
            stale_timeout_seconds=60,
        )
        task = kanban_db.get_task(conn, task_id)
        runs = kanban_db.list_runs(conn, task_id)
    finally:
        conn.close()

    assert result.stale == []
    assert result.crashed == []
    assert terminations == []
    assert task is not None
    assert task.status == "done"
    assert task.consecutive_failures == 0
    assert task.last_failure_error is None
    assert runs[-1].outcome == "completed"
    assert runs[-1].summary == "Reviewer approved stale run."


def test_dispatch_recovers_recorded_role_result_before_expired_claim_reclaim(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_REVIEWER
    from hermes_cli.discord_worker_state import record_codex_worker_result

    board = dwb.start_direct_goal(thread_id="recover-expired-review", goal="Review expired result")
    now = int(time.time())
    conn = kanban_db.connect(board=board.slug)
    try:
        task_id = kanban_db.create_task(
            conn,
            title="Review",
            assignee=ROLE_REVIEWER,
            tenant=board.slug,
            workspace_kind="dir",
            workspace_path=str(tmp_path / "repo"),
        )
        claimed = kanban_db.claim_task(conn, task_id)
        assert claimed is not None
        conn.execute(
            """
            UPDATE tasks
               SET worker_pid = ?, last_heartbeat_at = ?, claim_expires = ?
             WHERE id = ?
            """,
            (999_999_999, now - 500, now - 1, task_id),
        )
        conn.execute(
            """
            UPDATE task_runs
               SET worker_pid = ?, started_at = ?, last_heartbeat_at = ?, claim_expires = ?
             WHERE id = ?
            """,
            (999_999_999, now - 500, now - 500, now - 1, claimed.current_run_id),
        )
        conn.commit()
        record_codex_worker_result(
            task_id,
            board=board.slug,
            result=SimpleNamespace(
                backend="codex",
                final_text=json.dumps(
                    {
                        "status": "approved",
                        "summary": "Reviewer approved expired run.",
                        "findings": [],
                        "new_tasks": [],
                    }
                ),
                error=None,
            ),
        )
        terminations = []
        monkeypatch.setattr(
            kanban_db,
            "_terminate_reclaimed_worker",
            lambda *args, **kwargs: terminations.append(args) or {"pid_terminated": False},
        )
        monkeypatch.setattr(kanban_db, "_pid_alive", lambda _pid: False)
        monkeypatch.setattr(kanban_db, "_classify_worker_exit", lambda _pid: ("unknown", None))

        result = kanban_db.dispatch_once(conn, board=board.slug, max_spawn=0)
        task = kanban_db.get_task(conn, task_id)
        runs = kanban_db.list_runs(conn, task_id)
    finally:
        conn.close()

    assert result.reclaimed == 0
    assert result.crashed == []
    assert terminations == []
    assert task is not None
    assert task.status == "done"
    assert task.consecutive_failures == 0
    assert task.last_failure_error is None
    assert runs[-1].outcome == "completed"
    assert runs[-1].summary == "Reviewer approved expired run."


def test_dispatch_recovers_blocked_dead_pid_recorded_role_result(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_REVIEWER
    from hermes_cli.discord_worker_state import record_codex_worker_result

    board = dwb.start_direct_goal(thread_id="recover-blocked-review", goal="Review blocked result")
    conn = kanban_db.connect(board=board.slug)
    try:
        task_id = kanban_db.create_task(
            conn,
            title="Review",
            assignee=ROLE_REVIEWER,
            tenant=board.slug,
            workspace_kind="dir",
            workspace_path=str(tmp_path / "repo"),
        )
        claimed = kanban_db.claim_task(conn, task_id)
        assert claimed is not None
        conn.execute(
            """
            UPDATE tasks
               SET status = 'blocked',
                   claim_lock = NULL,
                   claim_expires = NULL,
                   worker_pid = NULL,
                   current_run_id = ?,
                   consecutive_failures = 2,
                   last_failure_error = ?
             WHERE id = ?
            """,
            (claimed.current_run_id, "pid 4141493 not alive", task_id),
        )
        conn.execute(
            """
            UPDATE task_runs
               SET status = 'crashed', outcome = 'crashed', error = ?
             WHERE id = ?
            """,
            ("pid 4141493 not alive", claimed.current_run_id),
        )
        conn.commit()
        record_codex_worker_result(
            task_id,
            board=board.slug,
            result=SimpleNamespace(
                backend="codex",
                final_text=json.dumps(
                    {
                        "status": "approved",
                        "summary": "Reviewer approved after blocked reclaim.",
                        "findings": [],
                        "new_tasks": [],
                    }
                ),
                error=None,
            ),
        )

        result = kanban_db.dispatch_once(conn, board=board.slug, max_spawn=0)
        task = kanban_db.get_task(conn, task_id)
        runs = kanban_db.list_runs(conn, task_id)
    finally:
        conn.close()

    assert result.crashed == []
    assert result.auto_blocked == []
    assert task is not None
    assert task.status == "done"
    assert task.last_failure_error is None
    assert runs[-1].outcome == "completed"
    assert runs[-1].summary == "Reviewer approved after blocked reclaim."


def test_dispatch_does_not_recover_blocked_dead_pid_from_stale_sidecar(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_REVIEWER
    from hermes_cli.discord_worker_state import record_codex_worker_result, write_codex_worker_state

    board = dwb.start_direct_goal(thread_id="recover-blocked-stale", goal="Review stale result")
    conn = kanban_db.connect(board=board.slug)
    try:
        task_id = kanban_db.create_task(
            conn,
            title="Review",
            assignee=ROLE_REVIEWER,
            tenant=board.slug,
            workspace_kind="dir",
            workspace_path=str(tmp_path / "repo"),
        )
        claimed = kanban_db.claim_task(conn, task_id)
        assert claimed is not None
        conn.execute(
            "UPDATE task_runs SET started_at = ? WHERE id = ?",
            (int(time.time()) + 60, claimed.current_run_id),
        )
        conn.execute(
            """
            UPDATE tasks
               SET status = 'blocked',
                   claim_lock = NULL,
                   claim_expires = NULL,
                   worker_pid = NULL,
                   current_run_id = ?,
                   consecutive_failures = 2,
                   last_failure_error = ?
             WHERE id = ?
            """,
            (claimed.current_run_id, "pid 4141493 not alive", task_id),
        )
        conn.execute(
            """
            UPDATE task_runs
               SET status = 'crashed', outcome = 'crashed', error = ?
             WHERE id = ?
            """,
            ("pid 4141493 not alive", claimed.current_run_id),
        )
        conn.commit()
        record_codex_worker_result(
            task_id,
            board=board.slug,
            result=SimpleNamespace(
                backend="codex",
                final_text=json.dumps(
                    {
                        "status": "approved",
                        "summary": "Stale reviewer approval.",
                        "findings": [],
                        "new_tasks": [],
                    }
                ),
                error=None,
            ),
        )
        write_codex_worker_state(task_id, board=board.slug, update={"updated_at": int(time.time()) - 60})

        result = kanban_db.dispatch_once(conn, board=board.slug, max_spawn=0)
        task = kanban_db.get_task(conn, task_id)
        runs = kanban_db.list_runs(conn, task_id)
    finally:
        conn.close()

    assert result.crashed == []
    assert task is not None
    assert task.status == "blocked"
    assert runs[-1].outcome == "crashed"


def test_opencode_adaptive_dev_reasoning_does_not_override_raw_config(monkeypatch):
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import config as config_mod

    monkeypatch.setenv("HERMES_CODEX_WORKER_REASONING", "medium")
    monkeypatch.setenv("HERMES_CODEX_WORKER_REASONING_SOURCE", "adaptive")
    monkeypatch.setattr(config_mod, "read_raw_config", lambda: {})

    assert worker._scheduled_opencode_worker_config() == {
        "simple_build_reasoning_level": "medium",
        "complex_build_reasoning_level": "medium",
    }

    monkeypatch.setattr(
        config_mod,
        "read_raw_config",
        lambda: {"coding_worker": {"simple_build_reasoning_level": "xhigh"}},
    )
    assert worker._scheduled_opencode_worker_config() is None


def test_role_extra_args_default_reasoning_by_role(monkeypatch):
    from hermes_cli import kanban_codex_worker as worker

    monkeypatch.delenv("HERMES_CODEX_WORKER_REASONING", raising=False)
    monkeypatch.delenv("HERMES_CODEX_WORKER_SERVICE_TIER", raising=False)

    assert worker._role_extra_args("planner")[1] == 'model_reasoning_effort="xhigh"'
    assert worker._role_extra_args("reviewer")[1] == 'model_reasoning_effort="xhigh"'
    assert worker._role_extra_args("foreman")[1] == 'model_reasoning_effort="xhigh"'
    assert worker._role_extra_args("dev")[1] == 'model_reasoning_effort="medium"'


def test_scheduled_runtime_metadata_attaches_to_worker_result(monkeypatch):
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli.discord_worker_boards import ROLE_DEV

    result = SimpleNamespace()
    monkeypatch.setenv("HERMES_CODEX_WORKER_REASONING", "low")
    monkeypatch.setenv("HERMES_CODEX_WORKER_SERVICE_TIER", "fast")

    worker._attach_scheduled_runtime(result, ROLE_DEV)

    assert result.service_tier == "fast"
    assert result.fast_mode is True
    assert result.run_profile == {
        "kind": "one_pass_build",
        "label": "1-pass build",
        "pass_count": 1,
        "plan_used": False,
        "passes": [{"name": "build", "agent": "dev", "reasoning": "low"}],
    }


def test_dev_role_uses_opencode_backend(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli.discord_worker_boards import ROLE_DEV
    from agent import opencode_worker as ow

    monkeypatch.setenv("HERMES_CODING_WORKER_BACKEND", "opencode")
    monkeypatch.setattr(worker, "record_codex_worker_result", lambda *args, **kwargs: None)
    calls = []

    def fake_run(prompt, workspace, **kwargs):
        calls.append((prompt, workspace, kwargs))
        return SimpleNamespace(
            final_text='{"status":"completed","summary":"ok","changed_files":[],"tests":[]}',
            error=None,
            interrupted=False,
            timed_out=False,
            should_retire=False,
            tool_iterations=1,
            thread_id="ses-build",
            turn_id="ses-build",
            backend="opencode",
            agents=["build"],
            plan_text="",
            exit_code=0,
        )

    monkeypatch.setattr(ow, "run_opencode_task", fake_run)

    result = worker._run_role_backend(
        "prompt",
        str(tmp_path),
        ROLE_DEV,
        task=SimpleNamespace(id="t_dev", title="Fix bug", body="Fix parser bug"),
        task_id="t_dev",
        board=None,
    )

    assert result.backend == "opencode"
    assert calls
    assert calls[0][1] == str(tmp_path)
    assert calls[0][2]["force_plan"] is False


def test_planner_role_uses_opencode_plan_agent(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli.discord_worker_boards import ROLE_PLANNER
    from agent import opencode_worker as ow

    monkeypatch.setenv("HERMES_CODING_WORKER_BACKEND", "opencode")
    monkeypatch.setattr(worker, "record_codex_worker_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        ow,
        "load_opencode_config",
        lambda: {"plan_agent": "plan", "complex_plan_reasoning_level": "xhigh"},
    )
    monkeypatch.setattr(
        ow,
        "run_opencode_task",
        lambda *args, **kwargs: pytest.fail("planner must not use build wrapper"),
    )
    calls = []

    def fake_single_pass(prompt, workspace, **kwargs):
        calls.append((prompt, workspace, kwargs))
        return SimpleNamespace(
            final_text=(
                '{"status":"planned","summary":"ok",'
                '"acceptance_criteria":["answer box is simplified"],'
                '"tasks":[{"title":"Clean answer box","body":"Do it",'
                '"priority":10,"parents":[]}],"blocker":null}'
            ),
            error=None,
            interrupted=False,
            timed_out=False,
            should_retire=False,
            tool_iterations=1,
            thread_id="ses-plan",
            turn_id="ses-plan",
            backend="opencode",
            agents=["plan"],
            plan_text="",
            exit_code=0,
        )

    monkeypatch.setattr(ow, "run_opencode_single_pass", fake_single_pass)

    result = worker._run_role_backend(
        "prompt",
        str(tmp_path),
        ROLE_PLANNER,
        task=SimpleNamespace(id="t_plan", title="Plan work", body=""),
        task_id="t_plan",
        board=None,
    )

    assert result.backend == "opencode"
    assert calls
    assert calls[0][1] == str(tmp_path)
    assert calls[0][2]["agent"] == "plan"
    assert calls[0][2]["reasoning_level"] == "xhigh"


def test_opencode_role_receives_sanitized_env(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli.discord_worker_boards import ROLE_DEV
    from agent import opencode_worker as ow

    control_values = {
        "HERMES_KANBAN_DB": str(tmp_path / "live" / "kanban.db"),
        "HERMES_KANBAN_BOARD": "discord-1512532369897160735",
        "HERMES_KANBAN_WORKSPACES_ROOT": str(tmp_path / "workspaces"),
        "HERMES_KANBAN_TASK": "task-1",
        "HERMES_KANBAN_RUN_ID": "run-1",
        "HERMES_KANBAN_CLAIM_LOCK": "claim-1",
        "HERMES_KANBAN_ROOT": str(tmp_path / "kanban-root"),
    }
    for key, value in control_values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("HERMES_CODING_WORKER_BACKEND", "opencode")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.setenv("OPENAI_API_KEY", "credential-survives")
    monkeypatch.setattr(worker, "record_codex_worker_result", lambda *args, **kwargs: None)
    calls = []

    def fake_run(prompt, workspace, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            final_text='{"status":"completed","summary":"ok","changed_files":[],"tests":[]}',
            error=None,
            interrupted=False,
            timed_out=False,
            should_retire=False,
            tool_iterations=1,
            thread_id="ses-build",
            turn_id="ses-build",
            backend="opencode",
            agents=["build"],
            plan_text="",
            exit_code=0,
        )

    monkeypatch.setattr(ow, "run_opencode_task", fake_run)

    result = worker._run_role_backend(
        "prompt",
        str(tmp_path),
        ROLE_DEV,
        task=SimpleNamespace(id="t_dev", title="Fix bug", body="Fix parser bug"),
        task_id="t_dev",
        board=None,
    )

    child_env = calls[0]["env"]
    assert result.backend == "opencode"
    for key in control_values:
        assert key not in child_env
        assert os.environ[key] == control_values[key]
    assert child_env["HERMES_HOME"] == str(tmp_path / "hermes-home")
    assert child_env["OPENAI_API_KEY"] == "credential-survives"
    assert child_env["HERMES_DISCORD_WORKER_READ_ONLY"] == "1"


def test_reviewer_role_uses_configured_opencode_backend(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli.discord_worker_boards import ROLE_REVIEWER
    from agent import opencode_worker as ow

    monkeypatch.setenv("HERMES_CODING_WORKER_BACKEND", "opencode")
    monkeypatch.setattr(worker, "record_codex_worker_result", lambda *args, **kwargs: None)
    calls = []

    def fake_run(prompt, workspace, **kwargs):
        calls.append((prompt, workspace, kwargs))
        return SimpleNamespace(
            final_text='{"status":"approved","summary":"ok","findings":[],"new_tasks":[]}',
            error=None,
            interrupted=False,
            timed_out=False,
            should_retire=False,
            tool_iterations=1,
            thread_id="ses-review",
            turn_id="ses-review",
            backend="opencode",
            agents=["build"],
            plan_text="",
            exit_code=0,
        )

    monkeypatch.setattr(ow, "run_opencode_task", fake_run)
    monkeypatch.setattr(
        worker,
        "_run_codex",
        lambda *args, **kwargs: pytest.fail("reviewer should use OpenCode when configured"),
    )

    result = worker._run_role_backend(
        "prompt",
        str(tmp_path),
        ROLE_REVIEWER,
        task=SimpleNamespace(id="t_review", title="Review", body=""),
        task_id="t_review",
        board=None,
    )

    assert result.backend == "opencode"
    assert result.error is None
    assert calls


def test_command_center_repair_foreman_runtime_uses_codex_with_codex_backend(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli.discord_worker_boards import ROLE_FOREMAN
    from agent import opencode_worker as ow

    monkeypatch.setenv("HERMES_CODING_WORKER_BACKEND", "codex")
    monkeypatch.setattr(worker, "record_codex_worker_result", lambda *args, **kwargs: None)
    calls = []

    def fake_codex(prompt, workspace, *args, **kwargs):
        calls.append((prompt, workspace, kwargs))
        return SimpleNamespace(
            final_text=(
                '{"status":"completed","summary":"ok",'
                '"actions":[],"verification":[],"changed_tasks":[]}'
            ),
            error=None,
            interrupted=False,
            timed_out=False,
            should_retire=False,
            tool_iterations=1,
            thread_id="ses-repair",
            turn_id="ses-repair",
            backend="codex",
            agents=["build"],
            plan_text="",
            exit_code=0,
        )

    monkeypatch.setattr(
        ow,
        "run_opencode_task",
        lambda *args, **kwargs: pytest.fail("repair foreman should use Codex when configured"),
    )
    monkeypatch.setattr(worker, "_run_codex", fake_codex)

    result = worker._run_role_backend(
        "prompt",
        str(tmp_path),
        ROLE_FOREMAN,
        task=SimpleNamespace(
            id="t_repair",
            title="Repair blocked board",
            body="Recover board",
            assignee=ROLE_FOREMAN,
            created_by="command-center-repair",
        ),
        task_id="t_repair",
        board=None,
    )

    assert result.backend == "codex"
    assert calls


def test_opencode_planner_output_creates_dev_ticket(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_PLANNER
    from agent import opencode_worker as ow

    board, task = _claimed_planner(monkeypatch, tmp_path)
    monkeypatch.setenv("HERMES_CODING_WORKER_BACKEND", "opencode")
    monkeypatch.setattr(worker, "record_codex_worker_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        ow,
        "load_opencode_config",
        lambda: {"plan_agent": "plan", "complex_plan_reasoning_level": "xhigh"},
    )
    monkeypatch.setattr(
        ow,
        "run_opencode_single_pass",
        lambda *args, **kwargs: SimpleNamespace(
            final_text=(
                '{"status":"planned","summary":"ok",'
                '"acceptance_criteria":["answer box is simplified"],'
                '"tasks":[{"title":"Clean answer box","body":"Do it",'
                '"priority":10,"parents":[]}],"blocker":null}'
            ),
            error=None,
            backend="opencode",
            agents=["plan"],
            tool_iterations=1,
            thread_id="ses-plan",
            turn_id="ses-plan",
        ),
    )

    result = worker._run_role_backend(
        "prompt",
        str(tmp_path / "repo"),
        ROLE_PLANNER,
        task=task,
        task_id=task.id,
        board=board.slug,
    )
    payload = worker._parse_json(result.final_text)

    conn = kanban_db.connect(board=board.slug)
    try:
        worker._apply_role_output(
            conn,
            task.id,
            ROLE_PLANNER,
            payload,
            board=board.slug,
            workspace=str(tmp_path / "repo"),
            expected_run_id=task.current_run_id,
        )
        dev_tasks = [
            item for item in kanban_db.list_tasks(conn, include_archived=False)
            if item.assignee == "dev"
        ]
    finally:
        conn.close()

    assert [item.title for item in dev_tasks] == ["R1: Clean answer box"]


def test_planner_output_replaces_bootstrap_request_criteria(monkeypatch, tmp_path):
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_PLANNER

    board, task = _claimed_planner(monkeypatch, tmp_path)
    old_request_body = (
        "Act as the planner for this board. Implement the approval activation so "
        "the Discord worker board starts in planning and produces dev tickets."
    )
    dwb._update_worker_meta(
        board.slug,
        {
            **board.worker,
            "criteria": [{"text": old_request_body, "active": True}],
        },
    )
    payload = {
        "status": "planned",
        "summary": "Planned one step.",
        "acceptance_criteria": [
            "Answer box is simplified.",
            "Answer box is simplified.",
            "Verification is recorded.",
        ],
        "tasks": [{"title": "Clean answer box", "body": "Do it.", "priority": 20}],
    }

    conn = kanban_db.connect(board=board.slug)
    try:
        worker._apply_role_output(
            conn,
            task.id,
            ROLE_PLANNER,
            payload,
            board=board.slug,
            workspace=str(tmp_path / "repo"),
            expected_run_id=task.current_run_id,
        )
    finally:
        conn.close()

    worker_meta = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert worker_meta["criteria"] == [
        {"text": "Answer box is simplified.", "active": True},
        {"text": "Verification is recorded.", "active": True},
    ]
    assert worker_meta["criteria_source"] == "planner"
    assert old_request_body not in json.dumps(worker_meta["criteria"])


def test_docker_runner_logs_immediate_registry_failure(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_workers as workers
    from hermes_cli import kanban_db

    board, task = _claimed_planner(monkeypatch, tmp_path)

    class Proc:
        pid = 9876

        def poll(self):
            return 125

    def fake_popen(cmd, cwd, stdout, stderr, env, start_new_session):
        stdout.write(b"docker: error from registry: denied\n")
        stdout.flush()
        return Proc()

    monkeypatch.setattr(
        workers,
        "_worker_config",
        lambda: {
            "backend": "codex",
            "runner": "docker",
            "docker_image": "ghcr.io/nousresearch/hermes-codex-worker:latest",
            "codex_home_root": str(tmp_path / "homes"),
        },
    )
    monkeypatch.setattr(workers.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(workers.subprocess, "Popen", fake_popen)

    with pytest.raises(RuntimeError, match="exited immediately with code 125"):
        workers.spawn_codex_worker(task, str(tmp_path / "repo"), board=board.slug)

    log = kanban_db.read_worker_log(task.id, board=board.slug)
    assert log is not None
    assert "ghcr.io/nousresearch/hermes-codex-worker:latest" in log
    assert "error from registry: denied" in log


def test_docker_runner_mounts_gh_config_read_only(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_workers as workers

    board, task = _claimed_planner(monkeypatch, tmp_path)
    real_home = tmp_path / "real-home"
    gh_dir = real_home / ".config" / "gh"
    gh_dir.mkdir(parents=True)
    (gh_dir / "hosts.yml").write_text("github.com:\n", encoding="utf-8")
    captured = {}

    class Proc:
        pid = 9876

        def poll(self):
            return None

    def fake_popen(cmd, cwd, stdout, stderr, env, start_new_session):
        captured.update({"cmd": cmd, "env": env})
        return Proc()

    monkeypatch.setenv("HOME", str(real_home))
    monkeypatch.delenv("GH_CONFIG_DIR", raising=False)
    monkeypatch.setattr(
        workers,
        "_worker_config",
        lambda: {
            "backend": "codex",
            "runner": "docker",
            "docker_image": "ghcr.io/nousresearch/hermes-codex-worker:latest",
            "codex_home_root": str(tmp_path / "homes"),
        },
    )
    monkeypatch.setattr(workers.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(workers.subprocess, "Popen", fake_popen)

    workers.spawn_codex_worker(task, str(tmp_path / "repo"), board=board.slug)

    assert captured["env"]["GH_CONFIG_DIR"] == "/gh-config"
    assert "-v" in captured["cmd"]
    assert f"{gh_dir.resolve()}:/gh-config:ro" in captured["cmd"]
    assert "-e" in captured["cmd"]
    assert "GH_CONFIG_DIR" in captured["cmd"]
    assert "GH_CONFIG_DIR=/gh-config" not in captured["cmd"]


def test_docker_runner_uses_absolute_runtime_script(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_workers as workers

    board, task = _claimed_planner(monkeypatch, tmp_path)
    captured = {}

    class Proc:
        pid = 9876

        def poll(self):
            return None

    def fake_popen(cmd, cwd, stdout, stderr, env, start_new_session):
        captured.update({"cmd": cmd, "env": env, "cwd": cwd})
        return Proc()

    monkeypatch.setattr(
        workers,
        "_worker_config",
        lambda: {
            "backend": "codex",
            "runner": "docker",
            "docker_image": "ghcr.io/nousresearch/hermes-codex-worker:latest",
            "codex_home_root": str(tmp_path / "homes"),
        },
    )
    monkeypatch.setattr(workers.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(workers.subprocess, "Popen", fake_popen)

    workers.spawn_codex_worker(task, str(tmp_path / "repo"), board=board.slug)

    assert captured["cmd"][-3:] == [
        "ghcr.io/nousresearch/hermes-codex-worker:latest",
        "python",
        "/hermes/hermes_cli/kanban_codex_worker.py",
    ]
    assert captured["env"]["PYTHONPATH"] == "/hermes"


def test_docker_runner_forwards_public_frontend_env_only(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_workers as workers

    board, task = _claimed_planner(monkeypatch, tmp_path)
    captured = {}

    class Proc:
        pid = 9876

        def poll(self):
            return None

    def fake_popen(cmd, cwd, stdout, stderr, env, start_new_session):
        captured.update({"cmd": cmd, "env": env})
        return Proc()

    monkeypatch.setenv("VITE_SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("VITE_SUPABASE_ANON_KEY", "public-anon-key")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "private-service-role")
    monkeypatch.setenv("DATABASE_URL", "postgres://private-db")
    monkeypatch.setattr(
        workers,
        "_worker_config",
        lambda: {
            "backend": "codex",
            "runner": "docker",
            "docker_image": "ghcr.io/nousresearch/hermes-codex-worker:latest",
            "codex_home_root": str(tmp_path / "homes"),
        },
    )
    monkeypatch.setattr(workers.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(workers.subprocess, "Popen", fake_popen)

    workers.spawn_codex_worker(task, str(tmp_path / "repo"), board=board.slug)

    assert "-e" in captured["cmd"]
    assert "VITE_SUPABASE_URL" in captured["cmd"]
    assert "VITE_SUPABASE_ANON_KEY" in captured["cmd"]
    assert "VITE_SUPABASE_URL=https://example.supabase.co" not in captured["cmd"]
    assert "VITE_SUPABASE_ANON_KEY=public-anon-key" not in captured["cmd"]
    assert "SUPABASE_SERVICE_ROLE_KEY" not in captured["cmd"]
    assert "DATABASE_URL" not in captured["cmd"]
    assert "private-service-role" not in captured["cmd"]
    assert "postgres://private-db" not in captured["cmd"]


def test_docker_runner_uses_read_broker_without_discord_credentials(monkeypatch, tmp_path):
    from hermes_cli import discord_worker_read
    from hermes_cli import kanban_codex_workers as workers
    from hermes_cli import kanban_db

    board, task = _claimed_planner(monkeypatch, tmp_path)
    captured = {}

    class Proc:
        pid = 9876

        def poll(self):
            return None

    def fake_popen(cmd, cwd, stdout, stderr, env, start_new_session):
        captured.update({"cmd": cmd, "env": env})
        return Proc()

    monkeypatch.setenv("DISCORD_BOT_TOKEN", "discord-token")
    monkeypatch.setenv("DISCORD_ADMIN_ACTIONS", "delete,pin")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "parent-codex-home"))
    monkeypatch.setenv("HERMES_CODEX_WORKER_CREDENTIAL_ID", "parent-cred")
    monkeypatch.setattr(
        discord_worker_read,
        "start_read_broker",
        lambda token: ("http://127.0.0.1:9", "broker-secret"),
    )
    monkeypatch.setattr(
        workers,
        "_worker_config",
        lambda: {
            "backend": "codex",
            "runner": "docker",
            "docker_image": "ghcr.io/nousresearch/hermes-codex-worker:latest",
            "codex_home_root": str(tmp_path / "homes"),
        },
    )
    monkeypatch.setattr(workers.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(workers.subprocess, "Popen", fake_popen)

    workers.spawn_codex_worker(task, str(tmp_path / "repo"), board=board.slug)

    assert "DISCORD_BOT_TOKEN" not in captured["env"]
    assert "DISCORD_ADMIN_ACTIONS" not in captured["env"]
    assert captured["env"]["CODEX_HOME"] == "/codex-home"
    assert captured["env"].get("HERMES_CODEX_WORKER_CREDENTIAL_ID") != "parent-cred"
    assert captured["env"]["HERMES_DISCORD_WORKER_READ_URL"] == "http://127.0.0.1:9"
    assert captured["env"]["HERMES_DISCORD_WORKER_READ_TOKEN"] == "broker-secret"
    assert captured["env"]["HERMES_DISCORD_WORKER_CONTROL_URL"] == "http://127.0.0.1:9"
    assert captured["env"]["HERMES_DISCORD_WORKER_CONTROL_TOKEN"] == "broker-secret"
    assert "HERMES_DISCORD_WORKER_READ_URL" in captured["cmd"]
    assert "HERMES_DISCORD_WORKER_READ_TOKEN" in captured["cmd"]
    assert "HERMES_DISCORD_WORKER_CONTROL_URL" in captured["cmd"]
    assert "HERMES_DISCORD_WORKER_CONTROL_TOKEN" in captured["cmd"]
    assert "broker-secret" not in captured["cmd"]
    assert "discord-token" not in captured["cmd"]
    assert "parent-cred" not in captured["cmd"]
    assert "DISCORD_BOT_TOKEN" not in captured["cmd"]
    assert "DISCORD_ADMIN_ACTIONS" not in captured["cmd"]
    log = kanban_db.read_worker_log(task.id, board=board.slug)
    assert log is not None
    assert "discord-token" not in log
    assert "broker-secret" not in log
    assert "parent-cred" not in log


def test_worker_prompt_is_read_only_for_normal_roles(monkeypatch):
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli.discord_worker_boards import ROLE_DEV, ROLE_PLANNER, ROLE_REVIEWER

    monkeypatch.setattr(worker.kanban_db, "build_worker_context", lambda _conn, _task_id: "{}")
    monkeypatch.setattr(worker, "_build_reviewer_context", lambda _conn, _task_id: "reviewer compact")
    monkeypatch.setattr(worker, "_git_summary", lambda _workspace: "clean")

    for role in (ROLE_PLANNER, ROLE_DEV, ROLE_REVIEWER):
        prompt = worker._build_prompt(object(), "task-1", role)
        assert "python -m hermes_cli.discord_worker_read fetch-message" in prompt
        assert "python -m hermes_cli.discord_worker_read fetch-messages" in prompt
        assert "read-only" in prompt.lower()
        assert "finalizer/operator owns board and Discord mutation" in prompt
        assert "python -m hermes_cli.discord_worker_read discord-request" not in prompt
        assert "python -m hermes_cli.discord_worker_read update-board" not in prompt
        assert "python -m hermes_cli.discord_worker_read task-status" not in prompt
        assert "python -m hermes_cli.discord_worker_read sync-summary" not in prompt
        assert "exact host:port" in prompt
        assert "worker_frontend_smoke" in prompt
        assert "python -m hermes_cli.browser_preflight chromium" in prompt
        assert "do not install browsers" in prompt


def test_planner_output_links_parent_dependencies(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_PLANNER

    board, task = _claimed_planner(monkeypatch, tmp_path)
    payload = {
        "status": "planned",
        "summary": "Planned two steps.",
        "acceptance_criteria": ["done"],
        "tasks": [
            {
                "title": "R1: Build foundation",
                "body": "Do first.",
                "priority": 20,
                "parents": [],
            },
            {"title": "Wire feature", "body": "Do second.", "priority": 10, "parents": [0]},
        ],
    }

    conn = kanban_db.connect(board=board.slug)
    try:
        worker._apply_role_output(
            conn,
            task.id,
            ROLE_PLANNER,
            payload,
            board=board.slug,
            workspace=str(tmp_path / "repo"),
            expected_run_id=task.current_run_id,
        )
        tasks = kanban_db.list_tasks(conn, include_archived=False)
        dev_tasks = [item for item in tasks if item.assignee == "dev"]
        assert len(dev_tasks) == 2
        first = next(item for item in dev_tasks if item.title == "R1: Build foundation")
        second = next(item for item in dev_tasks if item.title == "R1: Wire feature")
        assert first.status == "ready"
        assert second.status == "todo"
        assert second.id in kanban_db.child_ids(conn, first.id)
    finally:
        conn.close()


def test_planner_output_cleans_created_tasks_when_completion_fails(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_PLANNER

    board, task = _claimed_planner(monkeypatch, tmp_path)
    payload = {
        "status": "planned",
        "summary": "Planned one step.",
        "tasks": [{"title": "Build foundation", "body": "Do it.", "priority": 20}],
    }

    def fail_complete(*args, **kwargs):
        raise RuntimeError("completion failed")

    monkeypatch.setattr(kanban_db, "complete_task", fail_complete)
    conn = kanban_db.connect(board=board.slug)
    try:
        with pytest.raises(RuntimeError, match="completion failed"):
            worker._apply_role_output(
                conn,
                task.id,
                ROLE_PLANNER,
                payload,
                board=board.slug,
                workspace=str(tmp_path / "repo"),
                expected_run_id=task.current_run_id,
            )
        dev_tasks = [
            item for item in kanban_db.list_tasks(conn, include_archived=False)
            if item.assignee == "dev"
        ]
    finally:
        conn.close()

    assert dev_tasks == []


def test_planner_output_persists_requirements_and_adds_dev_context_header(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_PLANNER

    board, task = _claimed_planner(monkeypatch, tmp_path)
    metadata = kanban_db.read_board_metadata(board.slug)
    board_worker = dict(metadata["discord_worker"])
    artifact_path = tmp_path / "plans" / "004-worker-pid-identity.md"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text("# Worker PID identity plan\n", encoding="utf-8")
    board_worker.update(
        {
            "context_pack_path": str(tmp_path / "context-pack.json"),
            "context_pack_markdown_path": str(tmp_path / "context-pack.md"),
            "discord_plan_artifacts": [
                {
                    "artifact_path": str(artifact_path),
                    "content_sha256": "sha256-plan",
                    "kind": "local_plan",
                }
            ],
        }
    )
    from hermes_cli import discord_worker_boards as dwb

    dwb._update_worker_meta(board.slug, board_worker)
    payload = {
        "status": "planned",
        "summary": "Planned one step.",
        "acceptance_criteria": ["done"],
        "requirements": [
            {
                "id": "REQ-1",
                "text": "Preserve Discord context",
                "source_message_ids": ["123456789012345678"],
                "owner_task_indices": [0],
                "required": True,
            }
        ],
        "tasks": [
            {
                "title": "Build context pack",
                "body": "Goal: implement.\nSuccess means: done.\nStop when: verified.",
                "priority": 20,
                "requirement_ids": ["REQ-1"],
            }
        ],
    }

    conn = kanban_db.connect(board=board.slug)
    try:
        worker._apply_role_output(
            conn,
            task.id,
            ROLE_PLANNER,
            payload,
            board=board.slug,
            workspace=str(tmp_path / "repo"),
            expected_run_id=task.current_run_id,
        )
        dev_task = [item for item in kanban_db.list_tasks(conn, include_archived=False) if item.assignee == "dev"][0]
    finally:
        conn.close()

    assert dev_task.body is not None
    assert "Context pack:" in dev_task.body
    assert str(tmp_path / "context-pack.md") in dev_task.body
    assert "Durable Discord plan artifact paths:" in dev_task.body
    assert str(artifact_path) in dev_task.body
    assert "Requirement IDs: REQ-1" in dev_task.body
    worker_meta = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert worker_meta["requirements"][0]["id"] == "REQ-1"
    assert worker_meta["requirements"][0]["owner_task_ids"] == [dev_task.id]


def test_reviewer_output_creates_next_round_dev_ticket(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_REVIEWER

    board = dwb.start_direct_goal(thread_id="review-followup", goal="Ship it")
    artifact_path = tmp_path / "plans" / "reviewer-followup.md"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text("# Reviewer follow-up plan\n", encoding="utf-8")
    dwb._update_worker_meta(
        board.slug,
        {
            **board.worker,
            "review_loop_count": 1,
            "discord_plan_artifacts": [
                {
                    "artifact_path": str(artifact_path),
                    "content_sha256": "reviewer-sha",
                    "kind": "local_plan",
                }
            ],
        },
    )
    conn = kanban_db.connect(board=board.slug)
    try:
        reviewer_id = kanban_db.create_task(
            conn,
            title="R1: Review Discord implementation",
            assignee=ROLE_REVIEWER,
            tenant=board.slug,
        )
        claimed = kanban_db.claim_task(conn, reviewer_id)
        assert claimed is not None

        worker._apply_role_output(
            conn,
            claimed.id,
            ROLE_REVIEWER,
            {
                "status": "changes_requested",
                "summary": "Needs follow-up.",
                "new_tasks": [
                    {"title": "R1: Fix follow-up", "body": "Do it.", "priority": 10}
                ],
            },
            board=board.slug,
            workspace=str(tmp_path / "repo"),
            expected_run_id=claimed.current_run_id,
        )
        dev_tasks = [
            item for item in kanban_db.list_tasks(conn, include_archived=False)
            if item.assignee == "dev"
        ]
    finally:
        conn.close()

    assert [item.title for item in dev_tasks] == ["R2: Fix follow-up"]
    assert dev_tasks[0].body is not None
    assert "Durable Discord plan artifact paths:" in dev_tasks[0].body
    assert str(artifact_path) in dev_tasks[0].body


def test_reviewer_pr_lifecycle_task_finalizes_without_dev_ticket(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_REVIEWER

    board = dwb.start_direct_goal(thread_id="review-pr-chore", goal="Ship it")
    calls = []
    monkeypatch.setattr(worker, "_ensure_pr", lambda board, workspace: calls.append((board, workspace)) or True)
    conn = kanban_db.connect(board=board.slug)
    try:
        reviewer_id = kanban_db.create_task(
            conn,
            title="R2: Review Discord implementation",
            assignee=ROLE_REVIEWER,
            tenant=board.slug,
        )
        claimed = kanban_db.claim_task(conn, reviewer_id)
        assert claimed is not None

        worker._apply_role_output(
            conn,
            claimed.id,
            ROLE_REVIEWER,
            {
                "status": "changes_requested",
                "summary": "Implementation is fine; PR branch is stale.",
                "new_tasks": [
                    {
                        "title": "R3: Update PR 239 with final branch state",
                        "body": "Push the worker branch, run gh pr checks --watch, and confirm the PR view is current.",
                        "priority": 10,
                    }
                ],
            },
            board=board.slug,
            workspace=str(tmp_path / "repo"),
            expected_run_id=claimed.current_run_id,
        )
        dev_tasks = [
            item for item in kanban_db.list_tasks(conn, include_archived=False)
            if item.assignee == "dev"
        ]
    finally:
        conn.close()

    meta = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert dev_tasks == []
    assert calls == [(board.slug, str(tmp_path / "repo"))]
    assert meta["phase"] == "complete"
    assert meta["goal_status"] == "done"


def test_reviewer_pr_lifecycle_filter_keeps_live_provenance_followup(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_REVIEWER

    board = dwb.start_direct_goal(thread_id="review-live-provenance", goal="Ship live cron pickup")
    conn = kanban_db.connect(board=board.slug)
    try:
        reviewer_id = kanban_db.create_task(
            conn,
            title="R2: Review Discord implementation",
            assignee=ROLE_REVIEWER,
            tenant=board.slug,
        )
        claimed = kanban_db.claim_task(conn, reviewer_id)
        assert claimed is not None

        worker._apply_role_output(
            conn,
            claimed.id,
            ROLE_REVIEWER,
            {
                "status": "changes_requested",
                "summary": "Live provenance is missing.",
                "new_tasks": [
                    {
                        "title": "Verify live pickup provenance",
                        "body": "Goal: verify and record the active runtime path, source of truth, and live pickup evidence.",
                        "priority": 10,
                    }
                ],
            },
            board=board.slug,
            workspace=str(tmp_path / "repo"),
            expected_run_id=claimed.current_run_id,
        )
        dev_tasks = [
            item for item in kanban_db.list_tasks(conn, include_archived=False)
            if item.assignee == "dev"
        ]
    finally:
        conn.close()

    assert [item.title for item in dev_tasks] == ["R1: Verify live pickup provenance"]


def test_reviewer_pr_lifecycle_task_filter_keeps_real_code_followup(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_REVIEWER

    board = dwb.start_direct_goal(thread_id="review-mixed-followup", goal="Ship it")
    conn = kanban_db.connect(board=board.slug)
    try:
        reviewer_id = kanban_db.create_task(
            conn,
            title="R2: Review Discord implementation",
            assignee=ROLE_REVIEWER,
            tenant=board.slug,
        )
        claimed = kanban_db.claim_task(conn, reviewer_id)
        assert claimed is not None

        worker._apply_role_output(
            conn,
            claimed.id,
            ROLE_REVIEWER,
            {
                "status": "changes_requested",
                "summary": "Needs one code fix and one PR chore.",
                "new_tasks": [
                    {"title": "Update PR 239", "body": "Push branch and wait for checks.", "priority": 10},
                    {"title": "Fix failing CI test", "body": "Goal: repair the failing unit test.", "priority": 9},
                ],
            },
            board=board.slug,
            workspace=str(tmp_path / "repo"),
            expected_run_id=claimed.current_run_id,
        )
        dev_tasks = [
            item for item in kanban_db.list_tasks(conn, include_archived=False)
            if item.assignee == "dev"
        ]
    finally:
        conn.close()

    assert [item.title for item in dev_tasks] == ["R1: Fix failing CI test"]


def test_reviewer_approval_blocks_board_when_pr_publication_fails(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_REVIEWER

    board = dwb.start_direct_goal(thread_id="review-pr-failed", goal="Ship it")
    conn = kanban_db.connect(board=board.slug)
    try:
        reviewer_id = kanban_db.create_task(
            conn,
            title="Review",
            assignee=ROLE_REVIEWER,
            created_by="test",
            tenant=board.slug,
        )
        reviewer = kanban_db.claim_task(conn, reviewer_id)
        assert reviewer is not None
        monkeypatch.setattr(worker, "_ensure_pr", lambda *args, **kwargs: False)

        worker._apply_role_output(
            conn,
            reviewer_id,
            ROLE_REVIEWER,
            {"status": "approved", "summary": "Looks good."},
            board=board.slug,
            workspace=str(tmp_path / "repo"),
            expected_run_id=reviewer.current_run_id,
        )
    finally:
        conn.close()

    meta = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert meta["phase"] == "blocked"
    assert meta["goal_status"] == "blocked"


def test_dev_blocked_output_marks_discord_board_blocked(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_DEV

    board = dwb.start_direct_goal(thread_id="dev-blocked", goal="Ship it")
    conn = kanban_db.connect(board=board.slug)
    try:
        task_id = kanban_db.create_task(
            conn,
            title="Implement change",
            assignee=ROLE_DEV,
            tenant=board.slug,
        )
        claimed = kanban_db.claim_task(conn, task_id)
        assert claimed is not None

        worker._apply_role_output(
            conn,
            claimed.id,
            ROLE_DEV,
            {
                "status": "blocked",
                "summary": "Workspace unavailable.",
                "blocker": "Workspace is not a git repository.",
            },
            board=board.slug,
            workspace=str(tmp_path / "repo"),
            expected_run_id=claimed.current_run_id,
        )

        blocked = kanban_db.get_task(conn, task_id)
    finally:
        conn.close()

    worker_meta = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert blocked is not None
    assert blocked.status == "blocked"
    assert worker_meta["goal_status"] == "blocked"
    assert worker_meta["phase"] == "blocked"


def test_dev_output_persists_handoff_manifest(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_DEV

    board = dwb.start_direct_goal(thread_id="dev-handoff", goal="Ship it")
    conn = kanban_db.connect(board=board.slug)
    try:
        task_id = kanban_db.create_task(conn, title="Implement", assignee=ROLE_DEV, tenant=board.slug)
        claimed = kanban_db.claim_task(conn, task_id)
        assert claimed is not None
        handoff = {
            "changed_files": ["app/page.tsx"],
            "tests": [{"command": "pnpm test", "result": "passed", "output": "ok"}],
            "verification": ["inspected UI"],
            "preview": {"url": "http://127.0.0.1:4173", "command": "pnpm preview --port 4173", "status": "passed"},
            "smoke_routes": ["/"],
            "known_warnings": ["none"],
            "notes": "ready for review",
        }
        worker._apply_role_output(
            conn,
            claimed.id,
            ROLE_DEV,
            {
                "status": "completed",
                "summary": "Done.",
                "changed_files": ["app/page.tsx"],
                "tests": [],
                "handoff": handoff,
            },
            board=board.slug,
            workspace=str(tmp_path / "repo"),
            expected_run_id=claimed.current_run_id,
        )
        run = kanban_db.list_runs(conn, task_id)[-1]
    finally:
        conn.close()

    assert run.metadata["handoff"] == handoff


def test_reviewer_prompt_uses_compact_context_and_parent_handoff(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_DEV, ROLE_REVIEWER

    board = dwb.start_direct_goal(
        thread_id="reviewer-compact",
        goal="Root goal with SECRET_FULL_BODY_SHOULD_NOT_APPEAR",
    )
    metadata = kanban_db.read_board_metadata(board.slug)
    board_worker = dict(metadata["discord_worker"])
    board_worker["criteria"] = ["Smoke exact preview port"]
    board_worker["requirements"] = [{"id": "REQ-1", "text": "Use handoff manifests"}]
    board_worker["context_pack_markdown_path"] = str(tmp_path / "context-pack.md")
    board_worker["context_pack_path"] = str(tmp_path / "context-pack.json")
    dwb._update_worker_meta(board.slug, board_worker)
    conn = kanban_db.connect(board=board.slug)
    try:
        dev_id = kanban_db.create_task(
            conn,
            title="Dev",
            body="FULL DEV BODY SHOULD NOT APPEAR " * 100,
            assignee=ROLE_DEV,
            tenant=board.slug,
        )
        dev = kanban_db.claim_task(conn, dev_id)
        assert dev is not None
        kanban_db.complete_task(
            conn,
            dev_id,
            summary="Dev complete.",
            metadata={"handoff": {"changed_files": ["src/app.ts"], "smoke_routes": ["/"]}},
            expected_run_id=dev.current_run_id,
        )
        reviewer_id = kanban_db.create_task(
            conn,
            title="Review",
            body="FULL REVIEW TASK BODY SHOULD NOT APPEAR " * 100,
            assignee=ROLE_REVIEWER,
            parents=[dev_id],
            tenant=board.slug,
        )
        prompt = worker._build_prompt(conn, reviewer_id, ROLE_REVIEWER)
    finally:
        conn.close()

    assert "Parent task handoff manifests" in prompt
    assert "src/app.ts" in prompt
    assert "Smoke exact preview port" in prompt
    assert "context-pack.md" in prompt
    assert "FULL DEV BODY SHOULD NOT APPEAR" not in prompt
    assert "FULL REVIEW TASK BODY SHOULD NOT APPEAR" not in prompt
    assert "Use parent task handoff manifests" in prompt


def test_planner_schema_uses_parents_not_depends_on():
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli.discord_worker_boards import ROLE_PLANNER

    schema = worker._schema_instructions(ROLE_PLANNER)
    assert '"parents"' in schema
    assert "depends_on" not in schema
    assert "fewest coherent dev tickets" in schema
    assert "Fold normal discovery, audit, polish, and verification" in schema
    assert "detailed, self-contained implementation brief" in schema
    assert "opens with Goal, Success means, and Stop when" in schema
    assert "ticket-specific acceptance criteria" in schema
    assert "include board-level criteria only when that ticket owns the whole outcome" in schema
    assert "Set Stop when to the concrete handoff point" in schema
    assert "deduplicated canonical board-level list" in schema


def test_planner_prompt_defaults_simple_jobs_to_one_dev_ticket():
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli.discord_worker_boards import ROLE_PLANNER

    frame = worker._role_outcome_frame(ROLE_PLANNER)
    schema = worker._schema_instructions(ROLE_PLANNER)

    assert "Simple or single-surface Hermes/Discord jobs default to exactly one dev ticket" in frame
    assert "For simple or single-surface Hermes/Discord jobs, default to exactly one dev ticket" in schema
    assert "Fold docs, runbook notes, migration verification" in schema
    assert "active-path evidence" in schema
    assert "owning implementation ticket" in schema


def test_planner_prompt_limits_standalone_tickets():
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli.discord_worker_boards import ROLE_PLANNER

    schema = worker._schema_instructions(ROLE_PLANNER)

    assert "Create standalone dev tickets only for truly independent implementation slices" in schema
    assert "user-requested separate deliverables" in schema
    assert "shared blockers/prerequisites affecting multiple tickets" in schema
    assert "Do not create extra assertion, telemetry, debug, hardening, or PR-check tickets" in schema
    assert "concrete unmet acceptance criterion" in schema


def test_reviewer_prompt_approves_when_only_optional_followups_remain():
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli.discord_worker_boards import ROLE_REVIEWER

    frame = worker._role_outcome_frame(ROLE_REVIEWER)
    schema = worker._schema_instructions(ROLE_REVIEWER)

    assert "If requirements are satisfied and only optional improvements remain" in frame
    assert "approve with empty new_tasks" in frame
    assert "Do not emit new_tasks for optional hardening, extra tests" in schema
    assert "PR lifecycle, docs polish, telemetry" in schema
    assert "routine active-path/code-island checks" in schema
    assert "nice-to-have cleanup when requirements are satisfied" in schema
    assert "set status to approved, keep new_tasks empty" in schema


def test_reviewer_prompt_preserves_real_gap_followups():
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli.discord_worker_boards import ROLE_REVIEWER

    schema = worker._schema_instructions(ROLE_REVIEWER)

    assert "Request a new round only for concrete acceptance gaps" in schema
    assert "evidenced regressions" in schema
    assert "real defects" in schema
    assert "requested behavior that is unmet" in schema
    assert "part of the requested behavior or acceptance criteria" in schema
    assert "follow-up dev task must explicitly ask dev to verify" in schema


def test_worker_role_frames_are_outcome_first():
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli.discord_worker_boards import ROLE_DEV, ROLE_PLANNER, ROLE_REVIEWER

    dev_frame = worker._role_outcome_frame(ROLE_DEV)
    reviewer_frame = worker._role_outcome_frame(ROLE_REVIEWER)
    reviewer_schema = worker._schema_instructions(ROLE_REVIEWER)

    assert "Goal: Complete the assigned Kanban ticket" in dev_frame
    assert "Success means:" in dev_frame
    assert "Stop when: Return the JSON completion" in dev_frame
    assert "Goal: Decide whether the board work satisfies" in reviewer_frame
    assert "Success means:" in reviewer_frame
    assert "Stop when: Return the JSON review verdict." in reviewer_frame
    assert "new_tasks body must be a self-contained follow-up brief" in reviewer_schema
    assert "opens with Goal, Success means, and Stop when" in reviewer_schema
    assert "Do not create dev tickets whose goal is to push a branch" in worker._schema_instructions(ROLE_PLANNER)
    assert "Do not emit new_tasks for pure PR lifecycle chores" in reviewer_schema
    assert "pre_review_readiness advisory only as evidence" in reviewer_schema
    assert "active runtime paths" in reviewer_schema
    assert "source of truth" in reviewer_schema
    assert "Never push to a remote branch" in worker._schema_instructions(ROLE_DEV)


def test_worker_pr_mutation_guard_blocks_push_and_pr_mutation(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli.discord_worker_boards import ROLE_DEV

    monkeypatch.setattr(worker.shutil, "which", lambda binary: "/bin/true")
    guard_env, guard_dir = worker._role_pr_mutation_guard_env(ROLE_DEV)
    assert guard_dir is not None
    env = os.environ.copy()
    env.update(guard_env)
    try:
        git_push = subprocess.run(["git", "push"], env=env, capture_output=True, text=True, timeout=10)
        gh_create = subprocess.run(["gh", "pr", "create"], env=env, capture_output=True, text=True, timeout=10)
        gh_repo_create = subprocess.run(
            ["gh", "--repo", "sligo-labs/PID", "pr", "create"],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        git_status = subprocess.run(["git", "status"], env=env, capture_output=True, text=True, timeout=10)
    finally:
        worker._cleanup_pr_mutation_guard(guard_dir)

    assert git_push.returncode == 126
    assert gh_create.returncode == 126
    assert gh_repo_create.returncode == 126
    assert "deterministic finalizer" in git_push.stderr
    assert "deterministic finalizer" in gh_create.stderr
    assert git_status.returncode == 0


def test_worker_prompt_mentions_discord_read_helper(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_PLANNER

    board, task = _claimed_planner(monkeypatch, tmp_path)
    conn = kanban_db.connect(board=board.slug)
    try:
        prompt = worker._build_prompt(conn, task.id, ROLE_PLANNER)
    finally:
        conn.close()

    assert "finalizer/operator owns board and Discord mutation" in prompt
    assert "Outcome frame:" in prompt
    assert "Goal: Convert the Kanban context into the smallest coherent implementation plan" in prompt
    assert "Success means:" in prompt
    assert "Stop when: Return the JSON plan or a concise blocker." in prompt
    assert "python -m hermes_cli.discord_worker_read fetch-message" in prompt
    assert "python -m hermes_cli.discord_worker_read fetch-messages" in prompt
    assert "python -m hermes_cli.discord_worker_read update-board" not in prompt
    assert "python -m hermes_cli.discord_worker_read discord-request" not in prompt


def test_foreman_role_prompt_and_guards_allow_repair_mutation(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_FOREMAN

    board = "foreman-repair-board"
    kanban_db.create_board(board, name="Foreman Repair Board")
    conn = kanban_db.connect(board=board)
    try:
        task_id = kanban_db.create_task(
            conn,
            title="Repair blocked board",
            body="Recover blocked worker-board tickets.",
            assignee=ROLE_FOREMAN,
            created_by="command-center-repair",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )
        prompt = worker._build_prompt(conn, task_id, ROLE_FOREMAN)
    finally:
        conn.close()

    assert "You are the Discord Kanban foreman worker" in prompt
    assert "safely mutate Kanban board/task state" in prompt
    assert "mark dispatch dirty" in prompt
    assert "retry, unblock, close, reassign" in prompt
    assert "Use Discord worker read/control broker access only when necessary" in prompt
    assert "not subject to the planner/dev/reviewer read-only" in prompt
    assert "Do not create code-change PRs" in prompt
    assert "follow_up_proposals" in prompt
    assert "Command Center self-improvement proposal/job" in prompt
    assert "durable repo fix discovered during repair" in prompt
    assert "Keep secrets" in prompt
    assert "Do not call mutation helpers" not in prompt
    assert worker._role_pr_mutation_guard_env(ROLE_FOREMAN) == ({}, None)
    assert worker._role_read_only_discord_env(ROLE_FOREMAN) == {}


def test_foreman_runtime_defaults_to_xhigh_normal():
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_codex_workers as workers
    from hermes_cli.discord_worker_boards import ROLE_FOREMAN

    os.environ.pop("HERMES_CODEX_WORKER_REASONING", None)
    os.environ.pop("HERMES_CODEX_WORKER_SERVICE_TIER", None)
    settings = workers._role_runtime_settings(ROLE_FOREMAN, {}, None)

    assert settings["reasoning"] == "xhigh"
    assert settings["service_tier"] == "normal"
    assert worker._worker_reasoning_effort(ROLE_FOREMAN) == "xhigh"


def test_foreman_completed_output_completes_repair_task_without_dev_checkpoint(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_FOREMAN

    board = "foreman-output-board"
    kanban_db.create_board(board, name="Foreman Output Board")
    conn = kanban_db.connect(board=board)
    try:
        task_id = kanban_db.create_task(
            conn,
            title="Repair blocked board",
            assignee=ROLE_FOREMAN,
            created_by="command-center-repair",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )
        claimed = kanban_db.claim_task(conn, task_id)
        assert claimed is not None
        checkpoint_calls: list[tuple[str, str, str]] = []
        monkeypatch.setattr(worker, "_checkpoint_commit", lambda workspace, task_id, summary: checkpoint_calls.append((workspace, task_id, summary)))
        payload = {
            "status": "completed",
            "summary": "Recovered board.",
            "actions": ["unblocked t1", "marked dispatch dirty"],
            "verification": ["dispatch picked t1"],
            "changed_tasks": [{"id": "t1", "action": "unblock", "status": "ready"}],
        }

        worker._apply_role_output(
            conn,
            task_id,
            ROLE_FOREMAN,
            payload,
            board=board,
            workspace=str(tmp_path),
            expected_run_id=claimed.current_run_id,
        )
        task = kanban_db.get_task(conn, task_id)
        run = kanban_db.latest_run(conn, task_id)
    finally:
        conn.close()

    assert checkpoint_calls == []
    assert task is not None
    assert task.status == "done"
    assert run is not None
    assert run.outcome == "completed"
    assert run.metadata["raw"] == payload
    assert run.metadata["actions"] == payload["actions"]
    assert run.metadata["verification"] == payload["verification"]
    assert run.metadata["changed_tasks"] == payload["changed_tasks"]


def test_run_codex_records_app_server_state(monkeypatch, tmp_path):
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli.discord_worker_boards import ROLE_PLANNER

    board, task = _claimed_planner(monkeypatch, tmp_path)
    workspace = tmp_path / "repo"
    workspace.mkdir()

    session_envs = []

    class FakeSession:
        def __init__(self, **kwargs):
            session_envs.append(dict(kwargs["env"]))
            self.on_event = kwargs["on_event"]

        def run_turn(self, prompt, turn_timeout):
            self.on_event(
                {
                    "method": "item/completed",
                    "params": {
                        "item": {
                            "type": "commandExecution",
                            "cwd": "/home/droid/secret",
                            "command": "cat /home/droid/secret/.env",
                        }
                    },
                }
            )
            return SimpleNamespace(
                final_text='{"status":"planned","summary":"ok","tasks":[]}',
                error=None,
                interrupted=False,
                timed_out=False,
                should_retire=False,
                tool_iterations=1,
                turn_id="turn-1",
                thread_id="thread-1",
            )

        def close(self):
            pass

    monkeypatch.setattr(worker, "CodexAppServerSession", FakeSession)

    result = worker._run_codex(
        "prompt",
        str(workspace),
        ROLE_PLANNER,
        task_id=task.id,
        board=board.slug,
    )

    assert result.turn_id == "turn-1"
    assert session_envs[0]["HERMES_DISCORD_WORKER_READ_ONLY"] == "1"
    assert session_envs[0]["HERMES_DISCORD_WORKER_CONTROL_URL"] == ""
    assert session_envs[0]["HERMES_DISCORD_WORKER_CONTROL_TOKEN"] == ""
    state = dwb.ticket_state_for_session("9001", task.id)["codex_state"]
    rendered = str(state)
    assert state["result"]["thread_id"] == "thread-1"
    assert state["events"][0]["item_type"] == "commandExecution"
    assert "/home/droid/secret" not in rendered
    assert "[REDACTED_PATH]" in rendered


def test_dev_role_backend_records_planner_ui_route_decision(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import config as config_mod
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli.config import DEFAULT_CONFIG
    from hermes_cli.discord_worker_boards import ROLE_DEV

    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    monkeypatch.setattr(config_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(worker, "_role_uses_opencode", lambda role, task: False)
    monkeypatch.setattr(worker, "_materialize_role_autoreview", lambda workspace, role: "")
    events = []
    captured = {}

    def fake_event(task_id, *, board, event):
        events.append(event)

    def fake_run_codex(prompt, workspace, role, *, task_id, board, ui_work_route=None):
        captured.update(
            {
                "prompt": prompt,
                "workspace": workspace,
                "role": role,
                "task_id": task_id,
                "board": board,
                "ui_work_route": ui_work_route,
            }
        )
        return SimpleNamespace(final_text="{}", error=None)

    monkeypatch.setattr(worker, "record_codex_worker_event", fake_event)
    monkeypatch.setattr(worker, "_run_codex", fake_run_codex)

    task = SimpleNamespace(
        title="R1: Smoke ui_visual_specialist route with tiny Command Center visual polish",
        body='Recorded planner route decision for this ticket: {"route":"ui_visual_specialist","rationale":"Command Center visual polish smoke"}',
        result=None,
    )

    worker._run_role_backend("prompt", str(tmp_path), ROLE_DEV, task=task, task_id="t-ui", board="b-ui")

    route = captured["ui_work_route"].metadata()
    assert route["selected_route"] == "ui_visual_specialist"
    assert route["selected_provider"] == "openrouter"
    assert route["selected_model"] == "z-ai/glm-5.2"
    assert route["route_decision_source"] == "planner"
    assert "selected_route: ui_visual_specialist" in captured["prompt"]
    assert "selected_model: z-ai/glm-5.2" in captured["prompt"]
    assert events[0]["method"] == "ui_work_route/decision"
    assert events[0]["params"]["route"]["selected_model"] == "z-ai/glm-5.2"


def test_run_codex_applies_ui_route_args_env_and_result_metadata(monkeypatch, tmp_path):
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import config as config_mod
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli.config import DEFAULT_CONFIG
    from hermes_cli.discord_worker_boards import ROLE_DEV
    from hermes_cli.ui_work_routing import resolve_ui_work_route
    from tools.environments.local import _HERMES_PROVIDER_ENV_FORCE_PREFIX

    board, task = _claimed_planner(monkeypatch, tmp_path)
    workspace = tmp_path / "repo"
    workspace.mkdir()
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(config_mod, "get_env_value", lambda key: "or-secret" if key == "OPENROUTER_API_KEY" else None)
    decision = resolve_ui_work_route(
        DEFAULT_CONFIG,
        task="Implement Command Center visual polish.",
        backend="codex",
        route_decision={"route": "ui_visual_specialist", "rationale": "visual polish"},
    )
    sessions = []

    class FakeSession:
        def __init__(self, **kwargs):
            sessions.append(kwargs)

        def run_turn(self, prompt, turn_timeout):
            return SimpleNamespace(
                final_text='{"status":"completed","summary":"ok","changed_files":[],"tests":[]}',
                error=None,
                interrupted=False,
                timed_out=False,
                should_retire=False,
                tool_iterations=1,
                turn_id="turn-ui",
                thread_id="thread-ui",
            )

        def close(self):
            pass

    monkeypatch.setattr(worker, "CodexAppServerSession", FakeSession)

    result = worker._run_codex(
        "prompt",
        str(workspace),
        ROLE_DEV,
        task_id=task.id,
        board=board.slug,
        ui_work_route=decision,
    )

    args = sessions[0]["extra_args"]
    env = sessions[0]["env"]
    assert 'model_provider="openrouter"' in args
    assert 'model="z-ai/glm-5.2"' in args
    assert env["HERMES_UI_WORK_ROUTE"] == "ui_visual_specialist"
    assert env["HERMES_UI_WORK_SELECTED_PROVIDER"] == "openrouter"
    assert env["HERMES_UI_WORK_SELECTED_MODEL"] == "z-ai/glm-5.2"
    assert env[f"{_HERMES_PROVIDER_ENV_FORCE_PREFIX}OPENROUTER_API_KEY"] == "or-secret"
    assert "OPENROUTER_API_KEY" not in env
    assert getattr(result, "ui_work_route")["selected_route"] == "ui_visual_specialist"
    state = dwb.ticket_state_for_session("9001", task.id)["codex_state"]
    assert state["result"]["ui_work_route"]["selected_provider"] == "openrouter"


def test_kanban_backend_child_env_scrubs_control_vars_without_mutating_role_env(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_worker as worker

    control_values = {
        "HERMES_KANBAN_DB": str(tmp_path / "live" / "kanban.db"),
        "HERMES_KANBAN_BOARD": "discord-1512532369897160735",
        "HERMES_KANBAN_WORKSPACES_ROOT": str(tmp_path / "workspaces"),
        "HERMES_KANBAN_TASK": "task-1",
        "HERMES_KANBAN_RUN_ID": "run-1",
        "HERMES_KANBAN_CLAIM_LOCK": "claim-1",
        "HERMES_KANBAN_ROOT": str(tmp_path / "kanban-root"),
    }
    for key, value in control_values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin")
    monkeypatch.setenv("OPENAI_API_KEY", "credential-survives")
    monkeypatch.setenv("VITE_PUBLIC_URL", "https://example.test")

    child_env = worker._backend_child_env({"HERMES_DISABLE_MCP": "1"})

    for key in control_values:
        assert key not in child_env
        assert os.environ[key] == control_values[key]
    assert child_env["HERMES_HOME"] == str(tmp_path / "hermes-home")
    assert child_env["PATH"] == "/usr/local/bin:/usr/bin"
    assert child_env["OPENAI_API_KEY"] == "credential-survives"
    assert child_env["VITE_PUBLIC_URL"] == "https://example.test"
    assert child_env["HERMES_DISABLE_MCP"] == "1"


def test_run_codex_passes_sanitized_replacement_env_to_app_server(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli.discord_worker_boards import ROLE_REVIEWER

    board, task = _claimed_planner(monkeypatch, tmp_path)
    workspace = tmp_path / "repo"
    workspace.mkdir()
    control_values = {
        "HERMES_KANBAN_DB": str(tmp_path / "live" / "kanban.db"),
        "HERMES_KANBAN_BOARD": board.slug,
        "HERMES_KANBAN_WORKSPACES_ROOT": str(tmp_path / "workspaces"),
        "HERMES_KANBAN_TASK": task.id,
        "HERMES_KANBAN_RUN_ID": "run-1",
        "HERMES_KANBAN_CLAIM_LOCK": "claim-1",
        "HERMES_KANBAN_ROOT": str(tmp_path / "kanban-root"),
    }
    for key, value in control_values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.setenv("OPENAI_API_KEY", "credential-survives")
    monkeypatch.setattr(worker, "record_codex_worker_result", lambda *args, **kwargs: None)
    sessions = []

    class FakeSession:
        def __init__(self, **kwargs):
            sessions.append(kwargs)

        def run_turn(self, prompt, turn_timeout):
            return SimpleNamespace(
                final_text='{"status":"approved","summary":"ok","findings":[]}',
                error=None,
                interrupted=False,
                timed_out=False,
                should_retire=False,
                tool_iterations=1,
                turn_id="turn-1",
                thread_id="thread-1",
            )

        def close(self):
            pass

    monkeypatch.setattr(worker, "CodexAppServerSession", FakeSession)

    result = worker._run_codex(
        "prompt",
        str(workspace),
        ROLE_REVIEWER,
        task_id=task.id,
        board=board.slug,
    )

    child_env = sessions[0]["env"]
    assert result.turn_id == "turn-1"
    assert sessions[0]["replace_env"] is True
    for key in control_values:
        assert key not in child_env
        assert os.environ[key] == control_values[key]
    assert child_env["HERMES_HOME"] == str(tmp_path / "hermes-home")
    assert child_env["OPENAI_API_KEY"] == "credential-survives"
    assert child_env["HERMES_DISCORD_WORKER_READ_ONLY"] == "1"
    assert child_env["HERMES_KANBAN_WORKSPACE"] == str(workspace)
    assert child_env["HERMES_DISABLE_MCP"] == "1"


def test_run_codex_retries_auth_failure_with_next_pool_credential(monkeypatch, tmp_path):
    from agent.credential_pool import STATUS_EXHAUSTED, load_pool
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli.discord_worker_boards import ROLE_PLANNER

    hermes_home = tmp_path / "hermes-home"
    _write_pool_auth(
        hermes_home,
        [
            {
                "id": "cred-1",
                "label": "primary",
                "auth_type": "oauth",
                "priority": 0,
                "source": "manual:device_code",
                "access_token": "access-1",
                "refresh_token": "refresh-1",
                "id_token": "id-1",
            },
            {
                "id": "cred-2",
                "label": "secondary",
                "auth_type": "oauth",
                "priority": 1,
                "source": "manual:device_code",
                "access_token": "access-2",
                "refresh_token": "refresh-2",
                "id_token": "id-2",
            },
        ],
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    codex_home = tmp_path / "worker-codex-home"
    _write_codex_auth(codex_home, access="access-1", refresh="refresh-1", id_token="id-1")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("HERMES_CODEX_WORKER_CREDENTIAL_ID", "cred-1")
    board, task = _claimed_planner(monkeypatch, tmp_path)
    workspace = tmp_path / "repo"
    workspace.mkdir()
    session_access_tokens = []

    class FakeSession:
        def __init__(self, **kwargs):
            payload = json.loads((Path(kwargs["codex_home"]) / "auth.json").read_text(encoding="utf-8"))
            session_access_tokens.append(payload["tokens"]["access_token"])

        def run_turn(self, prompt, turn_timeout):
            if len(session_access_tokens) == 1:
                return SimpleNamespace(
                    final_text="",
                    error="Codex authentication failed: refresh token was revoked.",
                    auth_failed=True,
                    interrupted=False,
                    timed_out=False,
                    should_retire=True,
                    tool_iterations=0,
                    turn_id=None,
                    thread_id=None,
                )
            return SimpleNamespace(
                final_text='{"status":"planned","summary":"ok","tasks":[]}',
                error=None,
                auth_failed=False,
                interrupted=False,
                timed_out=False,
                should_retire=False,
                tool_iterations=1,
                turn_id="turn-2",
                thread_id="thread-2",
            )

        def close(self):
            pass

    monkeypatch.setattr(worker, "CodexAppServerSession", FakeSession)
    monkeypatch.setattr(worker, "record_codex_worker_result", lambda *args, **kwargs: None)

    result = worker._run_codex(
        "prompt",
        str(workspace),
        ROLE_PLANNER,
        task_id=task.id,
        board=board.slug,
    )

    pool_entries = {entry.id: entry for entry in load_pool("openai-codex").entries()}
    payload = json.loads((codex_home / "auth.json").read_text(encoding="utf-8"))
    assert result.turn_id == "turn-2"
    assert session_access_tokens == ["access-1", "access-2"]
    assert os.environ["HERMES_CODEX_WORKER_CREDENTIAL_ID"] == "cred-2"
    assert payload["tokens"]["access_token"] == "access-2"
    assert pool_entries["cred-1"].last_status == STATUS_EXHAUSTED
    assert pool_entries["cred-1"].last_error_code == 401


def test_rotate_codex_worker_credential_uses_child_home_for_container_mount(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_worker as worker

    hermes_home = tmp_path / "hermes-home"
    _write_pool_auth(
        hermes_home,
        [
            {
                "id": "cred-1",
                "label": "primary",
                "auth_type": "oauth",
                "priority": 0,
                "source": "manual:device_code",
                "access_token": "access-1",
                "refresh_token": "refresh-1",
                "id_token": "id-1",
            },
            {
                "id": "cred-2",
                "label": "secondary",
                "auth_type": "oauth",
                "priority": 1,
                "source": "manual:device_code",
                "access_token": "access-2",
                "refresh_token": "refresh-2",
                "id_token": "id-2",
            },
        ],
    )
    codex_mount = tmp_path / "codex-mount"
    _write_codex_auth(codex_mount, access="access-1", refresh="refresh-1", id_token="id-1")
    (codex_mount / "sentinel.txt").write_text("keep", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("CODEX_HOME", str(codex_mount))
    monkeypatch.setenv("HERMES_CODEX_WORKER_CREDENTIAL_ID", "cred-1")
    monkeypatch.setenv("HERMES_CODEX_WORKER_CONTAINER_CODEX_HOME", "1")

    rotated = worker._rotate_codex_worker_credential_after_auth_failure(
        SimpleNamespace(error="Codex authentication failed")
    )

    next_home = codex_mount / ".rotated-credential-home"
    original_payload = json.loads((codex_mount / "auth.json").read_text(encoding="utf-8"))
    next_payload = json.loads((next_home / "auth.json").read_text(encoding="utf-8"))
    assert rotated is True
    assert os.environ["CODEX_HOME"] == str(next_home)
    assert (codex_mount / "sentinel.txt").read_text(encoding="utf-8") == "keep"
    assert original_payload["tokens"]["access_token"] == "access-1"
    assert next_payload["tokens"]["access_token"] == "access-2"
    assert not next_home.is_symlink()


def test_rotate_codex_worker_credential_disables_fallback_auth_copy(monkeypatch, tmp_path):
    from agent import codex_worker_auth
    from hermes_cli import kanban_codex_worker as worker

    captured = {}
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "worker-codex-home"))
    monkeypatch.setenv("HERMES_CODEX_WORKER_CREDENTIAL_ID", "cred-1")
    monkeypatch.setattr(
        codex_worker_auth,
        "mark_codex_worker_credential_auth_failed",
        lambda credential_id, *, message=None: True,
    )

    def fake_prepare(codex_home, **kwargs):
        captured.update(kwargs)
        return None

    monkeypatch.setattr(codex_worker_auth, "prepare_codex_worker_home", fake_prepare)

    rotated = worker._rotate_codex_worker_credential_after_auth_failure(
        SimpleNamespace(error="Codex authentication failed")
    )

    assert rotated is False
    assert captured["allow_fallback"] is False


def test_update_phase_refreshes_worker_updated_at(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db

    board, _task = _claimed_planner(monkeypatch, tmp_path)
    monkeypatch.setattr(worker.time, "time", lambda: 12345)

    worker._update_phase(board.slug, "complete", goal_status="done")

    meta = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert meta["phase"] == "complete"
    assert meta["goal_status"] == "done"
    assert meta["updated_at"] == 12345
    assert meta["terminal_reaction_sync_pending"] is True
    assert meta["terminal_summary_sync_pending"] is True
    assert meta["terminal_completion_message_pending"] is True


def test_pr_policy_defaults_auto_but_explicit_do_not_merge_sets_never(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db

    auto_board = dwb.start_direct_goal(thread_id="auto-pr-policy", goal="Implement and ship it")
    never_board = dwb.start_direct_goal(
        thread_id="never-pr-policy",
        goal="Implement this, open a PR at the end, but DO NOT merge it.",
    )

    auto_meta = kanban_db.read_board_metadata(auto_board.slug)["discord_worker"]
    never_meta = kanban_db.read_board_metadata(never_board.slug)["discord_worker"]
    assert auto_meta["pr_open_policy"] == "after_review_approval"
    assert auto_meta["merge_policy"] == "auto"
    assert never_meta["pr_open_policy"] == "after_review_approval"
    assert never_meta["merge_policy"] == "never"


def test_pr_policy_explicit_local_only_disables_pr_lifecycle(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db

    board = dwb.start_direct_goal(
        thread_id="local-only-policy",
        goal=(
            "Dev work stops at a local verified branch state; it does not open "
            "pull requests, push remote branches, wait on remote checks, or merge."
        ),
    )

    meta = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert meta["pr_open_policy"] == "never"
    assert meta["merge_policy"] == "never"


def test_ensure_pr_honors_local_only_criteria_over_stale_pr_metadata(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db

    board = dwb.start_direct_goal(
        thread_id="local-only-stale-metadata",
        goal="Implement and verify locally.",
        project_context={"github_url": "https://github.com/sligo-labs/PID.git"},
    )
    dwb._update_worker_meta(
        board.slug,
        {
            "criteria": [
                {
                    "text": (
                        "Dev work stops at a local verified branch state; it does not open "
                        "pull requests, push remote branches, wait on remote checks, or merge."
                    ),
                    "active": True,
                }
            ],
            "pr_open_policy": "after_review_approval",
            "merge_policy": "auto",
        },
    )
    workspace = tmp_path / "repo"
    workspace.mkdir()
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)

    assert worker._ensure_pr(board.slug, str(workspace)) is True

    meta = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert meta["pr_open_policy"] == "never"
    assert meta["merge_policy"] == "never"
    assert meta["pr_state"] == "not_needed"
    assert meta["pr_checks_status"] == "passed"
    assert meta["pr_blocker"] == ""
    assert meta["pr_merge_skipped_reason"] == "pr_open_policy_never"
    assert not any(cmd[:2] == ["git", "push"] for cmd in calls)
    assert not any(cmd[:3] == ["gh", "pr", "create"] for cmd in calls)


def test_ensure_pr_never_policy_opens_without_merging(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db

    board = dwb.start_direct_goal(
        thread_id="open-only-pr",
        goal="Open a PR at the end but DO NOT merge it.",
        project_context={"github_url": "https://github.com/sligo-labs/PID.git"},
    )
    workspace = tmp_path / "repo"
    workspace.mkdir()
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["gh", "pr", "list"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["gh", "pr", "create"]:
            return SimpleNamespace(returncode=0, stdout="https://github.com/sligo-labs/PID/pull/321\n", stderr="")
        if cmd[:3] == ["gh", "pr", "view"]:
            return SimpleNamespace(
                returncode=0,
                stdout=_pr_view_json(
                    number=321,
                    state="OPEN",
                    merge_state="UNSTABLE",
                    checks=[{"name": "ci", "status": "IN_PROGRESS", "conclusion": ""}],
                ),
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)

    assert worker._ensure_pr(board.slug, str(workspace)) is True

    meta = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert ["git", "push", "-u", "origin", "discord/open-only-pr"] in calls
    assert not any(cmd[:3] == ["gh", "pr", "merge"] for cmd in calls)
    assert meta["merge_policy"] == "never"
    assert meta["pr_state"] == "OPEN"
    assert meta["pr_checks_status"] == "pending"
    assert meta["pr_merge_skipped"] is True
    assert meta["pr_merge_skipped_reason"] == "never"
    assert meta["pr_blocker"] == ""
    assert "canonical_sync_state" not in meta


def test_ensure_pr_open_uses_sanitized_github_env_for_push_and_gh(
    monkeypatch, tmp_path
):
    from hermes_cli import github_remote
    from hermes_cli import kanban_codex_worker as worker

    isolated_home = tmp_path / "hermes-home" / "home"
    real_gh_config = tmp_path / "real-home" / ".config" / "gh"
    real_gh_config.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(isolated_home))
    monkeypatch.setenv("GH_TOKEN", "gho_secret")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.delenv("GH_CONFIG_DIR", raising=False)
    monkeypatch.setattr(
        github_remote,
        "get_github_cli_config_dir",
        lambda env: str(real_gh_config) if env.get("HOME") == str(isolated_home) else "",
    )
    calls: list[tuple[list[str], dict | None]] = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs.get("env")))
        if cmd == ["git", "remote", "-v"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:2] == ["git", "push"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["gh", "pr", "list"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["gh", "pr", "create"]:
            return SimpleNamespace(
                returncode=0,
                stdout="https://github.com/sligo-labs/PID/pull/321\n",
                stderr="",
            )
        if cmd[:3] == ["gh", "pr", "view"]:
            return SimpleNamespace(
                returncode=0,
                stdout=_pr_view_json(number=321, state="OPEN"),
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)

    assert worker._ensure_pr_open(
        {"summary": "Opened finalizer PR."},
        root=tmp_path,
        repo="sligo-labs/PID",
        branch="feature/pr-env",
        base="main",
        board="discord-pr-env",
    )

    env_by_cmd = {tuple(cmd[:3]): env for cmd, env in calls if env}
    for key in (("git", "push", "-u"), ("gh", "pr", "create")):
        env = env_by_cmd[key]
        assert env["GH_CONFIG_DIR"] == str(real_gh_config)
        assert "GH_TOKEN" not in env
        assert "GITHUB_TOKEN" not in env


def test_ensure_pr_open_blocks_pr_amend_when_origin_is_upstream_repo(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_worker as worker

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd == ["git", "remote", "-v"]:
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "origin\tgit@github.com:reserve-protocol/reserve-index-dtf.git (fetch)\n"
                    "origin\tgit@github.com:reserve-protocol/reserve-index-dtf.git (push)\n"
                ),
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)
    data = {
        "project_context": {
            "github_pr_amend": {
                "upstream_repo": "reserve-protocol/reserve-index-dtf",
                "head_repo": "sligo-droid/reserve-index-dtf",
                "head_ref": "feat/irrevocable-fee-recipients",
            }
        }
    }

    assert not worker._ensure_pr_open(
        data,
        root=tmp_path,
        repo="sligo-droid/reserve-index-dtf",
        branch="discord/pr-amend-target",
        base="feat/irrevocable-fee-recipients",
        board="discord-pr-amend-target",
    )

    assert data["pr_error"] == (
        "PR-amend checkout origin mismatch: origin repo reserve-protocol/reserve-index-dtf is not finalizer target repo "
        "sligo-droid/reserve-index-dtf. Upstream/base repo reserve-protocol/reserve-index-dtf is source/review "
        "context only; target repo must be PR head repo sligo-droid/reserve-index-dtf. Fix checkout origin before finalization."
    )
    assert not any(cmd[:3] == ["git", "push", "-u"] for cmd in calls)
    assert not any(cmd[:2] == ["gh", "pr"] for cmd in calls)


def test_ensure_pr_open_blocks_pr_amend_when_origin_fetch_is_head_but_push_is_upstream(
    monkeypatch, tmp_path
):
    from hermes_cli import kanban_codex_worker as worker

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd == ["git", "remote", "-v"]:
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "origin\tgit@github.com:sligo-droid/reserve-index-dtf.git (fetch)\n"
                    "origin\tgit@github.com:reserve-protocol/reserve-index-dtf.git (push)\n"
                ),
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)
    data = {
        "project_context": {
            "github_pr_amend": {
                "upstream_repo": "reserve-protocol/reserve-index-dtf",
                "head_repo": "sligo-droid/reserve-index-dtf",
                "head_ref": "feat/irrevocable-fee-recipients",
            }
        }
    }

    assert not worker._ensure_pr_open(
        data,
        root=tmp_path,
        repo="sligo-droid/reserve-index-dtf",
        branch="discord/pr-amend-target",
        base="feat/irrevocable-fee-recipients",
        board="discord-pr-amend-target",
    )

    assert (
        "origin repo reserve-protocol/reserve-index-dtf is not finalizer target repo sligo-droid/reserve-index-dtf"
        in data["pr_error"]
    )
    assert not any(cmd[:3] == ["git", "push", "-u"] for cmd in calls)
    assert not any(cmd[:2] == ["gh", "pr"] for cmd in calls)


def test_ensure_pr_open_allows_pr_amend_when_origin_is_head_repo(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_worker as worker

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd == ["git", "remote", "-v"]:
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "origin\tgit@github.com:sligo-droid/reserve-index-dtf.git (fetch)\n"
                    "origin\tgit@github.com:sligo-droid/reserve-index-dtf.git (push)\n"
                ),
                stderr="",
            )
        if cmd[:3] == ["git", "push", "-u"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["gh", "pr", "list"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["gh", "pr", "create"]:
            return SimpleNamespace(returncode=0, stdout="https://github.com/sligo-droid/reserve-index-dtf/pull/7\n", stderr="")
        if cmd[:3] == ["gh", "pr", "view"]:
            return SimpleNamespace(returncode=0, stdout=_pr_view_json(number=7, state="OPEN"), stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)
    data = {
        "summary": "Opened finalizer PR.",
        "project_context": {
            "github_pr_amend": {
                "upstream_repo": "reserve-protocol/reserve-index-dtf",
                "head_repo": "sligo-droid/reserve-index-dtf",
                "head_ref": "feat/irrevocable-fee-recipients",
            }
        },
    }

    assert worker._ensure_pr_open(
        data,
        root=tmp_path,
        repo="sligo-droid/reserve-index-dtf",
        branch="discord/pr-amend-target",
        base="feat/irrevocable-fee-recipients",
        board="discord-pr-amend-target",
    )

    assert ["git", "push", "-u", "origin", "discord/pr-amend-target"] in calls
    pr_create = next(cmd for cmd in calls if cmd[:3] == ["gh", "pr", "create"])
    assert pr_create[pr_create.index("--repo") + 1] == "sligo-droid/reserve-index-dtf"
    assert pr_create[pr_create.index("--base") + 1] == "feat/irrevocable-fee-recipients"


def test_ensure_pr_uses_explicit_repo_base_and_head_from_project_context(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db

    board = dwb.start_direct_goal(
        thread_id="explicit-pr",
        goal="Ship explicit PR context",
        project_context={"github_url": "https://github.com/sligo-labs/PID.git", "base_branch": "develop"},
    )
    workspace = tmp_path / "repo"
    project_path = tmp_path / "canonical"
    workspace.mkdir()
    project_path.mkdir()
    dwb._update_worker_meta(board.slug, {"project_path": str(project_path)})
    calls = []
    view_states = ["OPEN", "MERGED"]

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["gh", "pr", "list"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["gh", "pr", "create"]:
            return SimpleNamespace(returncode=0, stdout="https://github.com/sligo-labs/PID/pull/123\n", stderr="")
        if cmd[:3] == ["gh", "pr", "view"]:
            state = view_states.pop(0)
            return SimpleNamespace(returncode=0, stdout=_pr_view_json(number=123, state=state), stderr="")
        if cmd[:3] == ["gh", "pr", "merge"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        sync_result = _canonical_sync_result(cmd)
        if sync_result is not None:
            return sync_result
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)

    assert worker._ensure_pr(board.slug, str(workspace)) is True

    pr_list = next(cmd for cmd in calls if cmd[:3] == ["gh", "pr", "list"])
    pr_create = next(cmd for cmd in calls if cmd[:3] == ["gh", "pr", "create"])
    assert pr_list[pr_list.index("--repo") + 1] == "sligo-labs/PID"
    assert pr_list[pr_list.index("--base") + 1] == "develop"
    assert pr_list[pr_list.index("--head") + 1] == "discord/explicit-pr"
    assert pr_create[pr_create.index("--repo") + 1] == "sligo-labs/PID"
    assert pr_create[pr_create.index("--base") + 1] == "develop"
    assert pr_create[pr_create.index("--head") + 1] == "discord/explicit-pr"
    pr_merge = next(cmd for cmd in calls if cmd[:3] == ["gh", "pr", "merge"])
    assert pr_merge[pr_merge.index("--repo") + 1] == "sligo-labs/PID"
    assert ["git", "push", "-u", "origin", "discord/explicit-pr"] in calls
    sync_commands = [
        ["git", "status", "--porcelain"],
        ["git", "fetch", "origin", "--prune"],
        ["git", "checkout", "develop"],
        ["git", "pull", "--ff-only", "origin", "develop"],
        ["git", "rev-parse", "HEAD"],
        ["git", "merge-base", "--is-ancestor", "abc123", "HEAD"],
    ]
    assert [cmd for cmd in calls if cmd in sync_commands] == sync_commands
    meta = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert meta["pr_url"] == "https://github.com/sligo-labs/PID/pull/123"
    assert meta["pr_state"] == "MERGED"
    assert meta["pr_merge_commit"] == "abc123"
    assert meta["canonical_sync_state"] == "synced"
    assert meta["canonical_sync_error"] == ""
    assert meta["canonical_sync_path"] == str(project_path)
    assert meta["canonical_sync_branch"] == "develop"
    assert meta["canonical_sync_head"] == "def456"
    assert meta["canonical_sync_merge_commit"] == "abc123"
    assert meta["canonical_synced_at"]


def test_ensure_pr_syncs_existing_worktree_when_base_branch_already_checked_out(
    monkeypatch, tmp_path
):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db

    board = dwb.start_direct_goal(
        thread_id="existing-worktree-pr",
        goal="Ship explicit PR context",
        project_context={"github_url": "https://github.com/sligo-labs/PID.git", "base_branch": "develop"},
    )
    workspace = tmp_path / "repo"
    project_path = tmp_path / "canonical"
    existing_path = tmp_path / "develop-worktree"
    workspace.mkdir()
    project_path.mkdir()
    existing_path.mkdir()
    dwb._update_worker_meta(board.slug, {"project_path": str(project_path)})
    calls = []
    view_states = ["OPEN", "MERGED"]

    def fake_run(cmd, **kwargs):
        cwd = Path(kwargs.get("cwd") or project_path)
        calls.append((cmd, cwd))
        if cmd[:3] == ["gh", "pr", "list"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["gh", "pr", "create"]:
            return SimpleNamespace(returncode=0, stdout="https://github.com/sligo-labs/PID/pull/123\n", stderr="")
        if cmd[:3] == ["gh", "pr", "view"]:
            state = view_states.pop(0)
            return SimpleNamespace(returncode=0, stdout=_pr_view_json(number=123, state=state), stderr="")
        if cmd[:3] == ["gh", "pr", "merge"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd == ["git", "status", "--porcelain"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd == ["git", "fetch", "origin", "--prune"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd == ["git", "checkout", "develop"]:
            return SimpleNamespace(
                returncode=128,
                stdout="",
                stderr=f"fatal: 'develop' is already used by worktree at '{existing_path}'",
            )
        if cmd == ["git", "worktree", "list", "--porcelain"]:
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    f"worktree {project_path}\n"
                    "HEAD aaaaaa\n"
                    "branch refs/heads/main\n\n"
                    f"worktree {existing_path}\n"
                    "HEAD existinghead\n"
                    "branch refs/heads/develop\n"
                ),
                stderr="",
            )
        if cmd == ["git", "pull", "--ff-only", "origin", "develop"]:
            assert cwd == existing_path
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd == ["git", "rev-parse", "HEAD"]:
            assert cwd == existing_path
            return SimpleNamespace(returncode=0, stdout="existinghead\n", stderr="")
        if cmd == ["git", "merge-base", "--is-ancestor", "abc123", "HEAD"]:
            assert cwd == existing_path
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)

    assert worker._ensure_pr(board.slug, str(workspace)) is True

    meta = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert (['git', 'checkout', 'develop'], project_path) in calls
    assert (['git', 'pull', '--ff-only', 'origin', 'develop'], existing_path) in calls
    assert meta["pr_state"] == "MERGED"
    assert meta["pr_merge_commit"] == "abc123"
    assert meta["canonical_sync_state"] == "synced_existing_worktree"
    assert meta["canonical_sync_error"] == ""
    assert meta["canonical_sync_path"] == str(existing_path)
    assert meta["canonical_sync_branch"] == "develop"
    assert meta["canonical_sync_head"] == "existinghead"
    assert meta["canonical_sync_merge_commit"] == "abc123"
    assert meta["canonical_synced_at"]


def test_ensure_pr_create_uses_concise_title_and_body(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_worker as worker

    giant_goal = (
        "Goal: implement concise PR copy.\n"
        "Acceptance criteria:\n- preserve this long list in Kanban only\n"
        "Rollback plan:\n- keep this section out of GitHub copy\n"
        + "full task body details " * 80
    )
    title, body = worker._build_worker_pr_copy(
        {
            "public_url": "https://dashboard.example.test/boards/concise-pr",
            "root_goal": giant_goal,
            "summary": "Implemented short PR copy from worker metadata.",
            "changed_files": ["agent/prompt_builder.py", "hermes_cli/kanban_codex_worker.py"],
            "tests": [{"command": "scripts/run_tests.sh tests/tools/test_kanban_tools.py", "result": "passed"}],
        },
        board="discord-concise-pr",
    )
    assert "\n" not in title
    assert len(title) <= 80
    assert title == "Discord worker: implement concise PR copy."
    assert "Acceptance criteria" not in title
    assert "Rollback" not in title
    assert body.startswith("Board: https://dashboard.example.test/boards/concise-pr\n\n")
    assert "## Summary\n" in body
    assert "## Verification\n" in body
    assert "scripts/run_tests.sh tests/tools/test_kanban_tools.py passed" in body
    assert "Acceptance criteria" not in body
    assert "Risk/rollback" not in body
    assert "full task body details" not in body


def test_ensure_pr_syncs_already_merged_pr(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db

    board = dwb.start_direct_goal(
        thread_id="already-merged-pr",
        goal="Ship already merged PR",
        project_context={"github_url": "https://github.com/sligo-labs/PID.git"},
    )
    workspace = tmp_path / "repo"
    project_path = tmp_path / "canonical"
    workspace.mkdir()
    project_path.mkdir()
    dwb._update_worker_meta(
        board.slug,
        {"project_path": str(project_path), "pr_url": "https://github.com/sligo-labs/PID/pull/123"},
    )
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["gh", "pr", "view"]:
            return SimpleNamespace(returncode=0, stdout=_pr_view_json(number=123, state="MERGED"), stderr="")
        sync_result = _canonical_sync_result(cmd, head="fedcba")
        if sync_result is not None:
            return sync_result
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)

    assert worker._ensure_pr(board.slug, str(workspace)) is True

    assert not any(cmd[:3] == ["gh", "pr", "merge"] for cmd in calls)
    assert ["git", "status", "--porcelain"] in calls
    meta = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert meta["pr_state"] == "MERGED"
    assert meta["canonical_sync_state"] == "synced"
    assert meta["canonical_sync_head"] == "fedcba"


@pytest.mark.parametrize(
    ("case", "project_exists", "sync_kwargs", "expected"),
    [
        ("missing", False, {}, "Canonical checkout missing or invalid"),
        ("dirty", True, {"dirty": True}, "Canonical checkout is dirty"),
        ("pull", True, {"pull_failed": True}, "Canonical checkout fast-forward pull failed"),
        ("ancestor", True, {"ancestor_failed": True}, "Canonical checkout does not contain PR merge commit"),
    ],
)
def test_ensure_pr_blocks_when_canonical_sync_fails(
    monkeypatch, tmp_path, case, project_exists, sync_kwargs, expected
):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db

    board = dwb.start_direct_goal(
        thread_id=f"sync-blocked-{case}",
        goal="Ship sync blocked PR",
        project_context={"github_url": "https://github.com/sligo-labs/PID.git"},
    )
    workspace = tmp_path / "repo"
    project_path = tmp_path / "canonical"
    workspace.mkdir()
    if project_exists:
        project_path.mkdir()
    dwb._update_worker_meta(board.slug, {"project_path": str(project_path)})
    calls = []
    view_states = ["OPEN", "MERGED"]

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["gh", "pr", "list"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["gh", "pr", "create"]:
            return SimpleNamespace(returncode=0, stdout="https://github.com/sligo-labs/PID/pull/123\n", stderr="")
        if cmd[:3] == ["gh", "pr", "view"]:
            return SimpleNamespace(returncode=0, stdout=_pr_view_json(number=123, state=view_states.pop(0)), stderr="")
        if cmd[:3] == ["gh", "pr", "merge"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        sync_result = _canonical_sync_result(cmd, **sync_kwargs)
        if sync_result is not None:
            return sync_result
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)

    assert worker._ensure_pr(board.slug, str(workspace)) is False

    meta = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert meta["pr_state"] == "MERGED"
    assert meta["canonical_sync_state"] == "blocked"
    assert expected in meta["canonical_sync_error"]
    assert meta["pr_error"] == meta["canonical_sync_error"]
    assert meta["pr_blocker"] == meta["pr_error"]
    if not project_exists:
        assert ["git", "status", "--porcelain"] not in calls


def test_ensure_pr_records_merge_checks_and_blocker(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db

    board = dwb.start_direct_goal(
        thread_id="blocked-pr",
        goal="Ship PR blocker facts",
        project_context={"github_url": "https://github.com/sligo-labs/PID.git"},
    )
    workspace = tmp_path / "repo"
    workspace.mkdir()

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["gh", "pr", "list"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["gh", "pr", "create"]:
            return SimpleNamespace(returncode=0, stdout="https://github.com/sligo-labs/PID/pull/125\n", stderr="")
        if cmd[:3] == ["gh", "pr", "view"]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "number": 125,
                        "url": "https://github.com/sligo-labs/PID/pull/125",
                        "state": "OPEN",
                        "mergeStateStatus": "BLOCKED",
                        "mergeable": "CONFLICTING",
                        "isDraft": False,
                        "reviewDecision": "REVIEW_REQUIRED",
                        "statusCheckRollup": [
                            {"name": "unit", "status": "COMPLETED", "conclusion": "SUCCESS"},
                            {"name": "lint", "status": "COMPLETED", "conclusion": "FAILURE"},
                        ],
                    }
                ),
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)

    assert worker._ensure_pr(board.slug, str(workspace)) is False

    meta = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert meta["pr_url"] == "https://github.com/sligo-labs/PID/pull/125"
    assert meta["pr_number"] == "125"
    assert meta["pr_state"] == "OPEN"
    assert meta["pr_merge_state"] == "BLOCKED"
    assert meta["pr_mergeable"] == "CONFLICTING"
    assert meta["pr_checks_status"] == "failed"
    assert meta["pr_checks_total"] == 2
    assert meta["pr_checks_failed"] == ["lint"]
    assert meta["pr_blocker"] == "checks failed: lint"
    assert not any(cmd[:3] == ["gh", "pr", "merge"] for cmd in calls)


def test_ensure_pr_waits_for_checks_before_merging(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_PR_MERGE_WAIT_SECONDS", "0")
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db

    board = dwb.start_direct_goal(
        thread_id="pending-pr",
        goal="Ship pending PR",
        project_context={"github_url": "https://github.com/sligo-labs/PID.git"},
    )
    workspace = tmp_path / "repo"
    workspace.mkdir()
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["gh", "pr", "list"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["gh", "pr", "create"]:
            return SimpleNamespace(returncode=0, stdout="https://github.com/sligo-labs/PID/pull/126\n", stderr="")
        if cmd[:3] == ["gh", "pr", "view"]:
            return SimpleNamespace(
                returncode=0,
                stdout=_pr_view_json(
                    number=126,
                    state="OPEN",
                    merge_state="UNSTABLE",
                    checks=[{"name": "ci", "status": "IN_PROGRESS", "conclusion": ""}],
                ),
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)

    assert worker._ensure_pr(board.slug, str(workspace)) is False

    meta = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert meta["pr_state"] == "OPEN"
    assert meta["pr_checks_status"] == "pending"
    assert meta["pr_blocker"] == "checks pending"
    assert not any(cmd[:3] == ["gh", "pr", "merge"] for cmd in calls)


def test_ensure_pr_merges_clean_pr_with_no_reported_checks_after_wait(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_PR_MERGE_WAIT_SECONDS", "0")
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db

    board = dwb.start_direct_goal(
        thread_id="no-check-pr",
        goal="Ship PR without checks",
        project_context={"github_url": "https://github.com/sligo-labs/PID.git"},
    )
    workspace = tmp_path / "repo"
    project_path = tmp_path / "canonical"
    workspace.mkdir()
    project_path.mkdir()
    dwb._update_worker_meta(board.slug, {"project_path": str(project_path)})
    calls = []
    view_states = iter(
        [
            _pr_view_json(number=129, state="OPEN", merge_state="CLEAN", checks=[]),
            _pr_view_json(number=129, state="MERGED", merge_state="CLEAN", checks=[]),
        ]
    )

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["gh", "pr", "list"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["gh", "pr", "create"]:
            return SimpleNamespace(returncode=0, stdout="https://github.com/sligo-labs/PID/pull/129\n", stderr="")
        if cmd[:3] == ["gh", "pr", "view"]:
            return SimpleNamespace(returncode=0, stdout=next(view_states), stderr="")
        if cmd[:3] == ["gh", "pr", "merge"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        sync_result = _canonical_sync_result(cmd)
        if sync_result is not None:
            return sync_result
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)

    assert worker._ensure_pr(board.slug, str(workspace)) is True

    assert any(cmd[:3] == ["gh", "pr", "merge"] for cmd in calls)
    meta = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert meta["pr_state"] == "MERGED"
    assert meta["pr_checks_status"] == "passed"
    assert meta["pr_checks_total"] == 0
    assert meta["pr_blocker"] == ""



def test_ensure_pr_polls_passed_unstable_before_merging(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_PR_MERGE_WAIT_SECONDS", "1")
    monkeypatch.setenv("HERMES_KANBAN_PR_MERGE_POLL_SECONDS", "0")
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db

    board = dwb.start_direct_goal(
        thread_id="unstable-pr",
        goal="Ship unstable PR",
        project_context={"github_url": "https://github.com/sligo-labs/PID.git"},
    )
    workspace = tmp_path / "repo"
    project_path = tmp_path / "canonical"
    workspace.mkdir()
    project_path.mkdir()
    dwb._update_worker_meta(board.slug, {"project_path": str(project_path)})
    calls = []
    view_states = iter(
        [
            _pr_view_json(number=127, state="OPEN", merge_state="UNSTABLE"),
            _pr_view_json(number=127, state="OPEN", merge_state="CLEAN"),
            _pr_view_json(number=127, state="MERGED", merge_state="CLEAN"),
        ]
    )

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["gh", "pr", "list"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["gh", "pr", "create"]:
            return SimpleNamespace(returncode=0, stdout="https://github.com/sligo-labs/PID/pull/127\n", stderr="")
        if cmd[:3] == ["gh", "pr", "view"]:
            return SimpleNamespace(returncode=0, stdout=next(view_states), stderr="")
        if cmd[:3] == ["gh", "pr", "merge"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        sync_result = _canonical_sync_result(cmd)
        if sync_result is not None:
            return sync_result
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)

    assert worker._ensure_pr(board.slug, str(workspace)) is True

    merge_index = next(index for index, cmd in enumerate(calls) if cmd[:3] == ["gh", "pr", "merge"])
    view_indices = [index for index, cmd in enumerate(calls) if cmd[:3] == ["gh", "pr", "view"]]
    clean_view_index = view_indices[1]
    assert clean_view_index < merge_index
    meta = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert meta["pr_state"] == "MERGED"
    assert meta["pr_merge_state"] == "CLEAN"
    assert meta["pr_checks_status"] == "passed"
    assert meta["pr_blocker"] == ""


def test_ensure_pr_falls_back_to_origin_remote_for_repo(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker

    board = dwb.start_direct_goal(thread_id="remote-pr", goal="Ship remote fallback")
    workspace = tmp_path / "repo"
    project_path = tmp_path / "canonical"
    workspace.mkdir()
    project_path.mkdir()
    dwb._update_worker_meta(board.slug, {"project_path": str(project_path)})
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd == ["git", "remote", "get-url", "origin"]:
            return SimpleNamespace(returncode=0, stdout="git@github.com:sligo-labs/PID.git\n", stderr="")
        if cmd[:3] == ["gh", "pr", "list"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["gh", "pr", "create"]:
            return SimpleNamespace(returncode=0, stdout="https://github.com/sligo-labs/PID/pull/124\n", stderr="")
        if cmd[:3] == ["gh", "pr", "view"]:
            return SimpleNamespace(returncode=0, stdout=_pr_view_json(number=124), stderr="")
        sync_result = _canonical_sync_result(cmd)
        if sync_result is not None:
            return sync_result
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)

    assert worker._ensure_pr(board.slug, str(workspace)) is True

    pr_create = next(cmd for cmd in calls if cmd[:3] == ["gh", "pr", "create"])
    assert pr_create[pr_create.index("--repo") + 1] == "sligo-labs/PID"
    assert pr_create[pr_create.index("--base") + 1] == "main"
    assert pr_create[pr_create.index("--head") + 1] == "discord/remote-pr"


def test_ensure_pr_blocks_local_only_remote_before_pr_creation(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db

    board = dwb.start_direct_goal(
        thread_id="local-remote-pr",
        goal="Ship remote preflight",
        project_context={"github_url": "https://github.com/sligo-labs/PID.git"},
    )
    workspace = tmp_path / "repo"
    workspace.mkdir()
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd == ["git", "remote", "get-url", "origin"]:
            return SimpleNamespace(returncode=0, stdout="/home/droid/hermes\n", stderr="")
        if cmd[:3] == ["git", "rev-list", "--count"]:
            return SimpleNamespace(returncode=0, stdout="1\n", stderr="")
        if cmd == ["git", "remote", "-v"]:
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "origin\t/home/droid/hermes (fetch)\n"
                    "origin\t/home/droid/hermes (push)\n"
                ),
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)

    assert worker._ensure_pr(board.slug, str(workspace)) is False

    meta = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert "only local/file remotes" in meta["pr_error"]
    assert "/home/droid/hermes" in meta["pr_error"]
    assert "not a GitHub token/auth problem" in meta["pr_error"]
    assert "git remote set-url origin" in meta["pr_error"]
    assert meta["pr_blocker"] == meta["pr_error"]
    assert not any(cmd[:2] == ["git", "push"] for cmd in calls)
    assert not any(cmd[:3] == ["gh", "pr", "create"] for cmd in calls)


def test_ensure_pr_prefers_checkout_remote_over_stale_project_context(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker

    board = dwb.start_direct_goal(
        thread_id="stale-context-pr",
        goal="Ship remote override",
        project_context={"project_github_url": "https://github.com/sligo-droid/PID"},
    )
    workspace = tmp_path / "repo"
    project_path = tmp_path / "canonical"
    workspace.mkdir()
    project_path.mkdir()
    dwb._update_worker_meta(board.slug, {"project_path": str(project_path)})
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd == ["git", "remote", "get-url", "origin"]:
            return SimpleNamespace(returncode=0, stdout="git@github.com:sligo-labs/PID.git\n", stderr="")
        if cmd[:3] == ["gh", "pr", "list"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["gh", "pr", "create"]:
            return SimpleNamespace(returncode=0, stdout="https://github.com/sligo-labs/PID/pull/124\n", stderr="")
        if cmd[:3] == ["gh", "pr", "view"]:
            return SimpleNamespace(returncode=0, stdout=_pr_view_json(number=124), stderr="")
        sync_result = _canonical_sync_result(cmd)
        if sync_result is not None:
            return sync_result
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)

    assert worker._ensure_pr(board.slug, str(workspace)) is True

    pr_list = next(cmd for cmd in calls if cmd[:3] == ["gh", "pr", "list"])
    pr_create = next(cmd for cmd in calls if cmd[:3] == ["gh", "pr", "create"])
    assert pr_list[pr_list.index("--repo") + 1] == "sligo-labs/PID"
    assert pr_create[pr_create.index("--repo") + 1] == "sligo-labs/PID"


def test_ensure_pr_explicit_target_repo_overrides_checkout_remote(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker

    board = dwb.start_direct_goal(
        thread_id="pr-amend-target",
        goal="Amend upstream PR through fork branch",
        project_context={
            "github_pr_target_repo": "sligo-droid/reserve-index-dtf",
            "base_branch": "feat/irrevocable-fee-recipients",
            "project_github_url": "https://github.com/reserve-protocol/reserve-index-dtf",
        },
    )
    workspace = tmp_path / "repo"
    project_path = tmp_path / "canonical"
    workspace.mkdir()
    project_path.mkdir()
    dwb._update_worker_meta(board.slug, {"project_path": str(project_path)})
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd == ["git", "remote", "get-url", "origin"]:
            return SimpleNamespace(returncode=0, stdout="git@github.com:reserve-protocol/reserve-index-dtf.git\n", stderr="")
        if cmd[:3] == ["gh", "pr", "list"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["gh", "pr", "create"]:
            return SimpleNamespace(returncode=0, stdout="https://github.com/sligo-droid/reserve-index-dtf/pull/7\n", stderr="")
        if cmd[:3] == ["gh", "pr", "view"]:
            return SimpleNamespace(returncode=0, stdout=_pr_view_json(number=7), stderr="")
        sync_result = _canonical_sync_result(cmd)
        if sync_result is not None:
            return sync_result
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)

    assert worker._ensure_pr(board.slug, str(workspace)) is True

    pr_create = next(cmd for cmd in calls if cmd[:3] == ["gh", "pr", "create"])
    assert pr_create[pr_create.index("--repo") + 1] == "sligo-droid/reserve-index-dtf"
    assert pr_create[pr_create.index("--base") + 1] == "feat/irrevocable-fee-recipients"
    assert pr_create[pr_create.index("--head") + 1] == "discord/pr-amend-target"


def test_ensure_pr_amend_uses_head_repo_and_never_upstream_for_pr_commands(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker

    board = dwb.start_direct_goal(
        thread_id="pr-amend-finalizer-target",
        goal="Amend upstream PR through fork branch",
        project_context={
            "github_pr_target_repo": "sligo-droid/reserve-index-dtf",
            "base_branch": "feat/irrevocable-fee-recipients",
            "project_github_url": "https://github.com/reserve-protocol/reserve-index-dtf",
            "github_pr_amend": {
                "upstream_repo": "reserve-protocol/reserve-index-dtf",
                "upstream_pr_number": "182",
                "head_repo": "sligo-droid/reserve-index-dtf",
                "head_ref": "feat/irrevocable-fee-recipients",
                "head_sha": "oldsha",
                "source_kind": "issue_comment",
            },
        },
    )
    workspace = tmp_path / "repo"
    project_path = tmp_path / "canonical"
    workspace.mkdir()
    project_path.mkdir()
    dwb._update_worker_meta(board.slug, {"project_path": str(project_path)})
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd == ["git", "remote", "get-url", "origin"]:
            return SimpleNamespace(returncode=0, stdout="git@github.com:reserve-protocol/reserve-index-dtf.git\n", stderr="")
        if cmd[:3] == ["gh", "pr", "list"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["gh", "pr", "create"]:
            return SimpleNamespace(returncode=0, stdout="https://github.com/sligo-droid/reserve-index-dtf/pull/7\n", stderr="")
        if cmd[:3] == ["gh", "pr", "view"]:
            return SimpleNamespace(returncode=0, stdout=_pr_view_json(number=7), stderr="")
        sync_result = _canonical_sync_result(cmd)
        if sync_result is not None:
            return sync_result
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)

    assert worker._ensure_pr(board.slug, str(workspace)) is True

    gh_pr_calls = [cmd for cmd in calls if cmd[:2] == ["gh", "pr"]]
    assert gh_pr_calls
    assert all(cmd[cmd.index("--repo") + 1] == "sligo-droid/reserve-index-dtf" for cmd in gh_pr_calls if "--repo" in cmd)
    assert all("reserve-protocol/reserve-index-dtf" not in cmd for cmd in gh_pr_calls)
    pr_create = next(cmd for cmd in gh_pr_calls if cmd[:3] == ["gh", "pr", "create"])
    assert pr_create[pr_create.index("--base") + 1] == "feat/irrevocable-fee-recipients"


def test_ensure_pr_amend_blocks_when_upstream_head_sha_does_not_advance(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db

    board = dwb.start_direct_goal(
        thread_id="pr-amend-unchanged-head",
        goal="Amend upstream PR through fork branch",
        project_context={
            "github_pr_target_repo": "sligo-droid/reserve-index-dtf",
            "base_branch": "feat/irrevocable-fee-recipients",
            "github_pr_amend": {
                "upstream_repo": "reserve-protocol/reserve-index-dtf",
                "upstream_pr_number": "182",
                "head_repo": "sligo-droid/reserve-index-dtf",
                "head_ref": "feat/irrevocable-fee-recipients",
                "head_sha": "oldsha",
                "source_kind": "review",
                "review_state": "CHANGES_REQUESTED",
                "requires_head_sha_advance": True,
            },
        },
    )
    workspace = tmp_path / "repo"
    project_path = tmp_path / "canonical"
    workspace.mkdir()
    project_path.mkdir()
    dwb._update_worker_meta(board.slug, {"project_path": str(project_path)})
    view_states = ["OPEN", "MERGED"]

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["gh", "pr", "list"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["gh", "pr", "create"]:
            return SimpleNamespace(returncode=0, stdout="https://github.com/sligo-droid/reserve-index-dtf/pull/7\n", stderr="")
        if cmd[:3] == ["gh", "pr", "view"] and "headRefOid" in cmd:
            return SimpleNamespace(returncode=0, stdout="oldsha\n", stderr="")
        if cmd[:3] == ["gh", "pr", "view"]:
            return SimpleNamespace(returncode=0, stdout=_pr_view_json(number=7, state=view_states.pop(0)), stderr="")
        if cmd[:3] == ["gh", "pr", "merge"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        sync_result = _canonical_sync_result(cmd)
        if sync_result is not None:
            return sync_result
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)

    assert worker._ensure_pr(board.slug, str(workspace)) is False

    meta = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert meta["pr_state"] == "MERGED"
    assert meta["pr_amend_head_advanced"] is False
    assert meta["pr_amend_upstream_head_sha"] == "oldsha"
    assert meta["pr_amend_trigger_head_sha"] == "oldsha"
    assert meta["pr_blocker"] == "PR-amend completion blocked: upstream PR head SHA did not advance from triggering review commit."


def test_ensure_pr_amend_succeeds_when_upstream_head_sha_advances(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db

    board = dwb.start_direct_goal(
        thread_id="pr-amend-advanced-head",
        goal="Amend upstream PR through fork branch",
        project_context={
            "github_pr_target_repo": "sligo-droid/reserve-index-dtf",
            "base_branch": "feat/irrevocable-fee-recipients",
            "github_pr_amend": {
                "upstream_repo": "reserve-protocol/reserve-index-dtf",
                "upstream_pr_number": "182",
                "head_repo": "sligo-droid/reserve-index-dtf",
                "head_ref": "feat/irrevocable-fee-recipients",
                "head_sha": "oldsha",
                "source_kind": "review",
                "review_state": "CHANGES_REQUESTED",
                "requires_head_sha_advance": True,
            },
        },
    )
    workspace = tmp_path / "repo"
    project_path = tmp_path / "canonical"
    workspace.mkdir()
    project_path.mkdir()
    dwb._update_worker_meta(board.slug, {"project_path": str(project_path)})
    view_states = ["OPEN", "MERGED"]

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["gh", "pr", "list"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["gh", "pr", "create"]:
            return SimpleNamespace(returncode=0, stdout="https://github.com/sligo-droid/reserve-index-dtf/pull/7\n", stderr="")
        if cmd[:3] == ["gh", "pr", "view"] and "headRefOid" in cmd:
            return SimpleNamespace(returncode=0, stdout="newsha\n", stderr="")
        if cmd[:3] == ["gh", "pr", "view"]:
            return SimpleNamespace(returncode=0, stdout=_pr_view_json(number=7, state=view_states.pop(0)), stderr="")
        if cmd[:3] == ["gh", "pr", "merge"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        sync_result = _canonical_sync_result(cmd)
        if sync_result is not None:
            return sync_result
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)

    assert worker._ensure_pr(board.slug, str(workspace)) is True

    meta = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert meta["pr_state"] == "MERGED"
    assert meta["pr_amend_head_advanced"] is True
    assert meta["pr_amend_upstream_head_sha"] == "newsha"
    assert meta["pr_blocker"] == ""


def test_ensure_pr_amend_finalizer_ignores_dev_worker_no_pr_text(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db

    board = dwb.start_direct_goal(
        thread_id="pr-amend-dev-text",
        goal="Amend upstream PR through fork branch",
        project_context={
            "github_pr_target_repo": "sligo-droid/reserve-index-dtf",
            "base_branch": "feat/irrevocable-fee-recipients",
            "github_pr_amend": {
                "upstream_repo": "reserve-protocol/reserve-index-dtf",
                "upstream_pr_number": "182",
                "head_repo": "sligo-droid/reserve-index-dtf",
                "head_ref": "feat/irrevocable-fee-recipients",
                "head_sha": "oldsha",
                "requires_head_sha_advance": True,
            },
        },
    )
    workspace = tmp_path / "repo"
    project_path = tmp_path / "canonical"
    workspace.mkdir()
    project_path.mkdir()
    dwb._update_worker_meta(
        board.slug,
        {
            "project_path": str(project_path),
            "latest_planner_request": "Dev workers do not open PRs/push/merge. Do not merge the upstream PR.",
        },
    )
    view_states = ["OPEN", "MERGED"]
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["gh", "pr", "list"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["gh", "pr", "create"]:
            return SimpleNamespace(returncode=0, stdout="https://github.com/sligo-droid/reserve-index-dtf/pull/7\n", stderr="")
        if cmd[:3] == ["gh", "pr", "view"] and "headRefOid" in cmd:
            return SimpleNamespace(returncode=0, stdout="newsha\n", stderr="")
        if cmd[:3] == ["gh", "pr", "view"]:
            return SimpleNamespace(returncode=0, stdout=_pr_view_json(number=7, state=view_states.pop(0)), stderr="")
        if cmd[:3] == ["gh", "pr", "merge"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        sync_result = _canonical_sync_result(cmd)
        if sync_result is not None:
            return sync_result
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)

    assert worker._ensure_pr(board.slug, str(workspace)) is True

    meta = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert meta["pr_open_policy"] == dwb.PR_OPEN_POLICY_AFTER_REVIEW_APPROVAL
    assert meta["merge_policy"] == dwb.MERGE_POLICY_AUTO
    assert meta.get("pr_skipped_no_changes") is not True
    assert meta["pr_amend_head_advanced"] is True
    assert any(cmd[:3] == ["gh", "pr", "create"] for cmd in calls)
    assert any(cmd[:3] == ["gh", "pr", "merge"] for cmd in calls)


def test_ensure_pr_skips_foreman_no_change_branch(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db

    board = dwb.start_direct_goal(
        thread_id="foreman-no-change",
        goal="Foreman escalation: resolve a Discord worker issue.",
        project_context={
            "project_github_url": "https://github.com/sligo-labs/PID.git"
        },
    )
    workspace = tmp_path / "repo"
    workspace.mkdir()
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd == ["git", "remote", "get-url", "origin"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="no remote")
        if cmd[:3] == ["git", "rev-list", "--count"]:
            return SimpleNamespace(returncode=0, stdout="0\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)

    assert worker._ensure_pr(board.slug, str(workspace)) is True

    meta = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert meta["pr_skipped_no_changes"] is True
    assert meta["pr_state"] == "not_needed"
    assert meta["pr_checks_status"] == "passed"
    assert meta["pr_blocker"] == ""
    assert not any(cmd[:3] == ["gh", "pr", "list"] for cmd in calls)
    assert not any(cmd[:2] == ["git", "push"] for cmd in calls)
    assert not any(cmd[:3] == ["gh", "pr", "create"] for cmd in calls)


def test_ensure_pr_records_error_when_repo_or_head_missing(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import DISCORD_WORKER_META_KEY
    from utils import atomic_json_write

    board = dwb.start_direct_goal(thread_id="missing-pr", goal="Cannot resolve")
    workspace = tmp_path / "repo"
    workspace.mkdir()
    metadata = kanban_db.read_board_metadata(board.slug)
    metadata[DISCORD_WORKER_META_KEY]["worker_branch"] = ""
    metadata.pop("db_path", None)
    atomic_json_write(kanban_db.board_metadata_path(board.slug), metadata, indent=2)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd == ["git", "remote", "get-url", "origin"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="no remote")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)

    assert worker._ensure_pr(board.slug, str(workspace)) is False

    meta = kanban_db.read_board_metadata(board.slug)[DISCORD_WORKER_META_KEY]
    assert meta["pr_error"] == "Cannot create PR: missing GitHub repository, worker branch"
    assert meta["pr_blocker"] == meta["pr_error"]
    assert meta["pr_checks_status"] == "not checked"
    assert meta["pr_merge_state"] == "unknown"
    assert not any(cmd[:3] == ["gh", "pr", "create"] for cmd in calls)


def test_ensure_pr_records_push_failure_before_create(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db

    board = dwb.start_direct_goal(
        thread_id="push-failed",
        goal="Cannot push",
        project_context={"github_url": "https://github.com/sligo-labs/PID.git"},
    )
    workspace = tmp_path / "repo"
    workspace.mkdir()
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["gh", "pr", "list"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:2] == ["git", "push"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="permission denied")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)

    assert worker._ensure_pr(board.slug, str(workspace)) is False

    meta = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert meta["pr_error"] == "permission denied"
    assert meta["pr_blocker"] == "permission denied"
    assert not any(cmd[:3] == ["gh", "pr", "create"] for cmd in calls)
