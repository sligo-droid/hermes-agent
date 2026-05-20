from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


def _home(monkeypatch, tmp_path: Path) -> Path:
    root = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_CODEX_WORKER_RUNNER", raising=False)
    return root


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


def test_codex_role_worker_defaults_to_host_runner(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_workers as workers

    board, task = _claimed_planner(monkeypatch, tmp_path)
    workspace = tmp_path / "repo"
    real_home = tmp_path / "real-home"
    gh_dir = real_home / ".config" / "gh"
    gh_dir.mkdir(parents=True)
    (gh_dir / "hosts.yml").write_text("github.com:\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(real_home))
    monkeypatch.delenv("GH_CONFIG_DIR", raising=False)
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

    monkeypatch.setattr(workers, "_worker_config", lambda: {"codex_home_root": str(tmp_path / "homes")})
    monkeypatch.setattr(workers.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(workers.subprocess, "Popen", fake_popen)

    pid = workers.spawn_codex_worker(task, str(workspace), board=board.slug)

    assert pid == 4321
    assert captured["cmd"][1:] == ["-m", "hermes_cli.kanban_codex_worker"]
    assert captured["cwd"] == str(workspace.resolve())
    assert captured["env"]["HERMES_CODEX_WORKER_ROLE"] == "planner"
    assert captured["env"]["HERMES_CODEX_WORKER_REASONING"] == "high"
    assert captured["env"]["HERMES_CODEX_WORKER_SERVICE_TIER"] == "normal"
    assert captured["env"]["HERMES_KANBAN_BOARD"] == board.slug
    assert captured["env"]["CODEX_HOME"].endswith("/homes/" + task.id)
    assert captured["env"]["GH_CONFIG_DIR"] == str(gh_dir)
    assert captured["start_new_session"] is True


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
                        },
                    ]
                },
            }
        )
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
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

    monkeypatch.setattr(workers, "_worker_config", lambda: {"codex_home_root": str(tmp_path / "homes")})
    monkeypatch.setattr(workers.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(workers.subprocess, "Popen", fake_popen)

    workers.spawn_codex_worker(task, str(workspace), board=board.slug)

    codex_home = Path(captured["env"]["CODEX_HOME"])
    payload = json.loads((codex_home / "auth.json").read_text())
    assert captured["env"]["HERMES_CODEX_WORKER_CREDENTIAL_ID"] == "cred-2"
    assert payload["tokens"]["access_token"] == "access-2"
    assert payload["tokens"]["refresh_token"] == "refresh-2"


def test_codex_role_worker_logs_scheduled_runtime_settings(monkeypatch, tmp_path):
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
    monkeypatch.setattr(workers.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(workers.subprocess, "Popen", lambda *args, **kwargs: Proc())

    workers.spawn_codex_worker(task, str(tmp_path / "repo"), board=board.slug)

    log = kanban_db.read_worker_log(task.id, board=board.slug)
    assert log is not None
    assert "[kanban dispatcher] scheduled Codex role worker: role=planner reasoning=xhigh mode=fast" in log


def test_planner_worker_env_carries_effective_opencode_backend(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_workers as workers
    from hermes_cli import kanban_db
    from agent import opencode_worker as ow

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


def test_role_extra_args_use_scheduled_runtime_env(monkeypatch):
    from hermes_cli import kanban_codex_worker as worker

    monkeypatch.setenv("HERMES_CODEX_WORKER_REASONING", "low")
    monkeypatch.setenv("HERMES_CODEX_WORKER_SERVICE_TIER", "fast")

    assert worker._role_extra_args("planner") == [
        "-c", 'model_reasoning_effort="low"',
        "-c", 'service_tier="fast"',
    ]


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


def test_reviewer_role_does_not_use_opencode_backend(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli.discord_worker_boards import ROLE_REVIEWER
    from agent import opencode_worker as ow

    monkeypatch.setenv("HERMES_CODING_WORKER_BACKEND", "opencode")
    monkeypatch.setattr(
        ow,
        "run_opencode_task",
        lambda *args, **kwargs: pytest.fail("reviewer must not call OpenCode"),
    )
    monkeypatch.setattr(
        worker,
        "_run_codex",
        lambda *args, **kwargs: SimpleNamespace(final_text="{}", error=None),
    )

    result = worker._run_role_backend(
        "prompt",
        str(tmp_path),
        ROLE_REVIEWER,
        task=SimpleNamespace(id="t_review", title="Review", body=""),
        task_id="t_review",
        board=None,
    )

    assert result.error is None


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

    assert [item.title for item in dev_tasks] == ["Clean answer box"]


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
    assert "GH_CONFIG_DIR=/gh-config" in captured["cmd"]


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
            {"title": "Build foundation", "body": "Do first.", "priority": 20, "parents": []},
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
        first = next(item for item in dev_tasks if item.title == "Build foundation")
        second = next(item for item in dev_tasks if item.title == "Wire feature")
        assert first.status == "ready"
        assert second.status == "todo"
        assert second.id in kanban_db.child_ids(conn, first.id)
    finally:
        conn.close()


def test_planner_schema_uses_parents_not_depends_on():
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli.discord_worker_boards import ROLE_PLANNER

    schema = worker._schema_instructions(ROLE_PLANNER)
    assert '"parents"' in schema
    assert "depends_on" not in schema
    assert "fewest coherent dev tickets" in schema
    assert "Do not create standalone discovery, audit, polish, or verification tickets" in schema
    assert "detailed, self-contained implementation brief" in schema
    assert "Ticket-specific acceptance criteria" in schema
    assert "do not copy the whole board-level list into every task" in schema
    assert "deduplicated canonical board-level list" in schema


def test_run_codex_records_app_server_state(monkeypatch, tmp_path):
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli.discord_worker_boards import ROLE_PLANNER

    board, task = _claimed_planner(monkeypatch, tmp_path)
    workspace = tmp_path / "repo"
    workspace.mkdir()

    class FakeSession:
        def __init__(self, **kwargs):
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
    state = dwb.ticket_state_for_session("9001", task.id)["codex_state"]
    rendered = str(state)
    assert state["result"]["thread_id"] == "thread-1"
    assert state["events"][0]["item_type"] == "commandExecution"
    assert "/home/droid/secret" not in rendered
    assert "[REDACTED_PATH]" in rendered
