from __future__ import annotations

import copy
import json
import os
import stat
import subprocess
import sys
import threading
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.transports.codex_app_server_session import TurnResult
from hermes_cli import plugins
from hermes_cli.config import DEFAULT_CONFIG
from hermes_cli.worktree_runtime import WorktreeRecord
from tools import async_delegation as ad
from tools import coding_worker_tool as cwt
from tools.process_registry import process_registry


_REAL_UI_VISUAL_ADVISOR = cwt._run_ui_visual_advisor


class FakeSession:
    instances = []
    results = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.closed = False
        self.run_calls = []
        self.auth_payload = None
        self.config_payload = None
        if kwargs.get("codex_home"):
            auth_path = Path(kwargs["codex_home"]) / "auth.json"
            if auth_path.exists():
                self.auth_payload = json.loads(auth_path.read_text(encoding="utf-8"))
            config_path = Path(kwargs["codex_home"]) / "config.toml"
            if config_path.exists():
                self.config_payload = tomllib.loads(config_path.read_text(encoding="utf-8"))
        FakeSession.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True

    def run_turn(self, **kwargs):
        self.run_calls.append(kwargs)
        if FakeSession.results:
            return FakeSession.results.pop(0)
        return TurnResult(
            final_text="Changed src/app.py and ran pytest.",
            thread_id="thread-1",
            turn_id="turn-1",
            tool_iterations=2,
        )


@pytest.fixture(autouse=True)
def _default_codex_backend(monkeypatch, tmp_path):
    from agent import opencode_worker as ow

    monkeypatch.setattr(ow, "load_coding_worker_backend", lambda: "codex")
    codex_home = tmp_path / "default-codex-home"
    codex_home.mkdir(exist_ok=True)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(
        cwt,
        "_run_ui_visual_advisor",
        lambda **kwargs: ("", {"advisor_invoked": False}),
    )


def _parent(tmp_path, api_mode="chat_completions"):
    return SimpleNamespace(
        api_mode=api_mode,
        session_cwd=str(tmp_path),
        session_key="discord:123",
        _touch_activity=lambda message: None,
    )


def _fable_parent(tmp_path):
    parent = _parent(tmp_path)
    parent._fable_implementation_turn = True
    return parent


def _init_git_worktree(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.email", "tests@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Hermes Tests"], cwd=path, check=True)
    (path / "src").mkdir()
    (path / "src" / "app.py").write_text("value = 1\n", encoding="utf-8")
    (path / "README.md").write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "test fixture"],
        cwd=path,
        check=True,
        stdout=subprocess.DEVNULL,
    )


def _drain_background_completion(timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_registry.completion_queue.empty():
            return process_registry.completion_queue.get_nowait()
        time.sleep(0.01)
    raise AssertionError("background coding-worker completion did not arrive")


def _reset_background_state() -> None:
    ad._reset_for_tests()
    with cwt._BACKGROUND_PARALLEL_WORKERS_GUARD:
        cwt._BACKGROUND_PARALLEL_WORKERS.clear()
        cwt._BACKGROUND_PARALLEL_RESULTS.clear()
    with cwt._PARALLEL_WORKER_RESERVATIONS_LOCK:
        cwt._PARALLEL_WORKER_RESERVATIONS.clear()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()


def _skill_block(name, body, directory=None, description=None):
    lines = [
        f'[IMPORTANT: The user launched this CLI session with the "{name}" skill preloaded. Treat its instructions as active guidance for the duration of this session unless the user overrides them.]'
    ]
    if description:
        lines.extend(["---", f"description: {description}", "---"])
    lines.append(body)
    if directory:
        lines.append(f"[Skill directory: {directory}]")
    return "\n".join(lines)


def _stub_general_coding(monkeypatch, content="General coding full body."):
    monkeypatch.setattr(
        cwt,
        "_load_general_coding_skill",
        lambda: cwt._SkillBlock(
            name="general-coding",
            body=content,
            summary="General coding rules.",
            directory="/tmp/general-coding",
        ),
    )


def test_requires_parent_agent():
    result = json.loads(cwt.delegate_coding_task(task="fix bug"))
    assert "requires a parent agent" in result["error"]


def test_unavailable_inside_codex_app_server(tmp_path):
    result = json.loads(
        cwt.delegate_coding_task(
            task="fix bug",
            parent_agent=_parent(tmp_path, api_mode="codex_app_server"),
        )
    )
    assert "unavailable" in result["error"]


def test_missing_turn_timeout_uses_config_default(monkeypatch):
    default_timeout = DEFAULT_CONFIG["coding_worker"]["turn_timeout_seconds"]
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"coding_worker": {}})

    assert cwt._load_coding_worker_timeout() == float(default_timeout)


def test_parallel_config_defaults_and_max_workers_floor(monkeypatch):
    assert DEFAULT_CONFIG["coding_worker"]["parallel"] == {
        "enabled": True,
        "max_workers": 3,
    }
    assert cwt.is_coding_worker_parallel_enabled(DEFAULT_CONFIG) is True
    assert cwt.get_coding_worker_parallel_max_workers(DEFAULT_CONFIG) == 3

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "coding_worker": {
                "parallel": {"enabled": False, "max_workers": 0},
            }
        },
    )
    assert cwt.is_coding_worker_parallel_enabled() is False
    assert cwt.get_coding_worker_parallel_max_workers() == 1

    assert DEFAULT_CONFIG["coding_worker"]["background"] == {
        "enabled": True,
        "max_concurrent": 3,
    }
    assert cwt.is_coding_worker_background_enabled(DEFAULT_CONFIG) is True
    assert cwt.get_coding_worker_background_max_concurrent(DEFAULT_CONFIG) == 3
    assert cwt.get_coding_worker_background_max_concurrent(
        {"coding_worker": {"background": {"max_concurrent": 0}}}
    ) == 1


def test_plain_call_returns_sequential_result_byte_identically(monkeypatch):
    expected = '{"success":true, "status":"completed", "custom":"unchanged bytes"}'
    monkeypatch.setattr(cwt, "_delegate_coding_task_impl", lambda **kwargs: expected)

    assert cwt.delegate_coding_task(task="plain sequential call") == expected


def test_runs_codex_app_server_session(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    (tmp_path / "AGENTS.md").write_text(
        "Always use the repo test wrapper. Open PRs and merge them yourself."
    )
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )
    monkeypatch.setattr(cwt, "_load_coding_worker_timeout", lambda: 123.0)
    parent = _parent(tmp_path)

    result = json.loads(
        cwt.delegate_coding_task(
            task="fix the parser",
            context="focus on src/parser.py",
            parent_agent=parent,
        )
    )

    assert result["success"] is True
    assert result["summary"] == "Changed src/app.py and ran pytest."
    assert result["cwd"] == str(tmp_path)
    assert FakeSession.instances[0].kwargs["cwd"] == str(tmp_path)
    assert FakeSession.instances[0].kwargs["env"]["HERMES_SESSION_KEY"] == "discord:123"
    assert "HERMES_CODEX_WORKER_NETWORK_ACCESS" not in FakeSession.instances[0].kwargs["env"]
    assert FakeSession.instances[0].kwargs["scope_kind"] == "coding-worker"
    assert FakeSession.instances[0].kwargs["scope_purpose"] == "Codex coding worker build pass"
    assert FakeSession.instances[0].kwargs["extra_args"] == [
        "-c",
        'model="gpt-5.6-sol"',
        "-c",
        'model_reasoning_effort="low"',
        "-c",
        'service_tier="normal"',
    ]
    assert FakeSession.instances[0].run_calls[0]["turn_timeout"] == 123.0
    prompt = FakeSession.instances[0].run_calls[0]["user_input"]
    assert "fix the parser" in prompt
    assert "focus on src/parser.py" in prompt
    assert "## Context from orchestrator" not in prompt
    assert "Scope guardrail:" not in prompt
    assert "Repository context loaded by Hermes" in prompt
    assert "## AGENTS.md" in prompt
    assert "Always use the repo test wrapper." in prompt
    assert "Worker boundary" in prompt
    assert "parent Hermes owns all git and PR lifecycle steps" in prompt
    assert "workspace-local autoreview helper" in prompt
    assert ".agents/skills/autoreview/scripts/autoreview --mode local" in prompt
    assert "after non-trivial code edits and focused checks" in prompt
    helper = tmp_path / ".agents" / "skills" / "autoreview" / "scripts" / "autoreview"
    assert helper.exists()
    assert os.access(helper, os.X_OK)
    assert prompt.index("Open PRs and merge them yourself") < prompt.index("Worker boundary")
    assert result["agents"] == ["build"]
    assert result["plan_used"] is False
    assert result["ui_work_route"]["matched"] is False
    assert "scope_check" not in result
    assert parent.turn_worker_runs == [
        {
            "backend": "codex",
            "model": "gpt-5.6-sol",
            "reasoning": "low",
            "model_tier": None,
        },
    ]


def test_worker_prompt_preserves_visual_requirement_and_dev_first_inspection(
    monkeypatch,
    tmp_path,
):
    FakeSession.instances = []
    FakeSession.results = []
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )
    parent = _parent(tmp_path)
    parent.visual_qa_requirement = {
        "level": "surface",
        "target": "responsive dashboard",
        "assertions": ["dashboard has no horizontal overflow"],
    }
    parent.project_inspection_candidates = [
        {
            "url": "http://127.0.0.1:5173/",
            "environment": "development",
            "location": "local",
        },
        {
            "url": "https://dev.example.test/",
            "environment": "development",
            "location": "external",
        },
        {
            "url": "https://prod.example.test/",
            "environment": "production",
            "location": "external",
        },
    ]

    result = json.loads(
        cwt.delegate_coding_task(
            task="Implement the responsive dashboard.",
            parent_agent=parent,
        )
    )

    assert result["success"] is True
    prompt = FakeSession.instances[0].run_calls[0]["user_input"]
    assert "Originating trusted visual-QA requirement" in prompt
    assert '"level":"surface"' in prompt
    assert "parent action orchestrator owns the transient `visual_qa`" in prompt
    assert "focused/context/responsive screenshot evidence" in prompt
    assert "do not invent or self-declare a receipt" in prompt
    local = prompt.index("http://127.0.0.1:5173/")
    external = prompt.index("https://dev.example.test/")
    production = prompt.index("https://prod.example.test/")
    assert local < external < production
    assert "Inspection order is dev-first" in prompt
    assert "only when connection, DNS, or navigation is unavailable" in prompt
    assert "Do not switch to production" in prompt
    assert "repository-local preview server" in prompt


def test_worker_reads_serialized_task_local_inspection_candidates(monkeypatch):
    from gateway import session_context

    monkeypatch.setattr(
        session_context,
        "get_session_env",
        lambda name, default="": (
            json.dumps(
                [
                    {
                        "url": "https://dev.example.test/",
                        "environment": "development",
                        "location": "external",
                    }
                ]
            )
            if name == "HERMES_PROJECT_INSPECTION_CANDIDATES"
            else default
        ),
    )

    candidates = cwt._originating_project_inspection_candidates(
        SimpleNamespace(),
    )

    assert candidates == [
        {
            "url": "https://dev.example.test/",
            "environment": "development",
            "location": "external",
        }
    ]


def test_context_pack_is_injected_before_task(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="fix the parser",
            relevant_files=[
                {"path": "src/parser.py:40", "note": "failure starts in parse_config"},
                {"path": "tests/test_parser.py", "note": "covers the regression"},
            ],
            approach="Patch the existing parser helper without adding a new abstraction.",
            constraints="Preserve the public config shape.",
            verification="Run scripts/run_tests.sh tests/test_parser.py.",
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True
    prompt = FakeSession.instances[0].run_calls[0]["user_input"]
    assert "## Context from orchestrator" in prompt
    assert "- `src/parser.py:40` — failure starts in parse_config" in prompt
    assert "Approach:\nPatch the existing parser helper" in prompt
    assert "Constraints:\nPreserve the public config shape." in prompt
    assert "Verification:\nRun scripts/run_tests.sh tests/test_parser.py." in prompt
    assert prompt.index("## Context from orchestrator") < prompt.index("\nTask:\nfix the parser")


def test_model_tier_and_reasoning_override_codex_runtime(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = [
        TurnResult(final_text="Plan", thread_id="thread-plan", turn_id="turn-plan"),
        TurnResult(final_text="Built", thread_id="thread-build", turn_id="turn-build"),
    ]
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )
    parent = _parent(tmp_path)

    result = json.loads(
        cwt.delegate_coding_task(
            task="fix the production auth race",
            model_tier="advanced",
            reasoning_effort="high",
            route_decision={"route": "ui_visual_specialist"},
            parent_agent=parent,
        )
    )

    assert result["success"] is True
    assert result["ui_work_route"]["selected_route"] == "ui_visual_specialist"
    assert result["ui_work_route"]["actual_model"] == "gpt-5.6-sol"
    assert result["ui_work_route"]["actual_reasoning_effort"] == "high"
    assert [session.kwargs["extra_args"] for session in FakeSession.instances] == [
        [
            "-c",
            'model="gpt-5.6-sol"',
            "-c",
            'model_reasoning_effort="high"',
            "-c",
            'service_tier="normal"',
        ],
        [
            "-c",
            'model="gpt-5.6-sol"',
            "-c",
            'model_reasoning_effort="high"',
            "-c",
            'service_tier="normal"',
        ],
    ]
    assert parent.turn_worker_runs == [
        {
            "backend": "codex",
            "model": "gpt-5.6-sol",
            "reasoning": "high",
            "model_tier": "advanced",
        },
    ]


def test_fast_named_tier_sets_codex_service_tier(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = [TurnResult(final_text="Built", thread_id="thread-build")]
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="fix the parser",
            model_tier="basic",
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True
    assert FakeSession.instances[0].kwargs["extra_args"][-2:] == [
        "-c",
        'service_tier="fast"',
    ]


def test_backend_error_marks_recorded_worker_run_failed(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = [TurnResult(error="worker backend failed")]
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )
    parent = _parent(tmp_path)

    result = json.loads(
        cwt.delegate_coding_task(
            task="fix the parser",
            parent_agent=parent,
        )
    )

    assert result["success"] is False
    assert parent.turn_worker_runs == [
        {
            "backend": "codex",
            "model": "gpt-5.6-sol",
            "reasoning": "low",
            "model_tier": None,
            "failed": True,
        },
    ]


def test_invalid_model_tier_returns_tool_error(tmp_path):
    result = json.loads(
        cwt.delegate_coding_task(
            task="fix the parser",
            model_tier="impossible",
            parent_agent=_parent(tmp_path),
        )
    )

    assert "Unknown model_tier 'impossible'" in result["error"]
    for tier in ("trivial", "basic", "intermediate", "advanced"):
        assert tier in result["error"]


def test_invalid_reasoning_effort_returns_tool_error(tmp_path):
    result = json.loads(
        cwt.delegate_coding_task(
            task="fix the parser",
            reasoning_effort="extreme",
            parent_agent=_parent(tmp_path),
        )
    )

    assert "Unknown reasoning_effort 'extreme'" in result["error"]


def test_coding_worker_schema_exposes_orchestrator_inputs():
    properties = cwt.CODING_WORKER_SCHEMA["parameters"]["properties"]

    assert properties["model_tier"]["type"] == "string"
    assert "enum" not in properties["model_tier"]
    assert properties["reasoning_effort"]["enum"] == [
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
        "ultra",
    ]
    assert "worker_tier" not in properties
    assert {"relevant_files", "approach", "constraints", "verification", "scope_paths"} <= set(
        properties
    )
    assert "_parallel_group" not in properties
    assert properties["background"]["default"] is False


def test_explicit_empty_scope_reserves_no_mutation_paths():
    assert cwt._reservation_scopes([]) == []
    assert cwt._reservation_scopes(None) == [PurePosixPath(".")]


def test_required_background_worker_marks_running_before_model_start(
    monkeypatch,
    tmp_path,
):
    _reset_background_state()
    calls = []
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_worktree(repo)

    class Ledger:
        def register_required_async_dispatch(self, work_id, **kwargs):
            calls.append(("register", work_id, kwargs))
            return {"dispatches": {kwargs["delegation_id"]: {"state": "registered"}}}

        def mark_required_async_dispatch_running(self, work_id, **kwargs):
            calls.append(("running", work_id, kwargs))
            return {"dispatches": {kwargs["delegation_id"]: {"state": "running"}}}

        def update_required_async_dispatch_recovery(self, work_id, **kwargs):
            calls.append(("checkpoint", work_id, kwargs))
            return {"dispatches": {kwargs["delegation_id"]: {"state": "registered"}}}

        def record_required_async_completion(self, work_id, **kwargs):
            calls.append(("complete", work_id, kwargs))
            return {
                "dispatches": {
                    kwargs["delegation_id"]: {"state": "terminal", "success": True}
                }
            }

    class ObservingSession(FakeSession):
        def run_turn(self, **kwargs):
            assert any(call[0] == "running" for call in calls)
            calls.append(("model_start",))
            return super().run_turn(**kwargs)

    cfg = copy.deepcopy(DEFAULT_CONFIG)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    monkeypatch.setattr(ad, "_required_async_ledger", lambda: Ledger())
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        ObservingSession,
    )
    monkeypatch.setattr(
        "hermes_cli.worker_autoreview.materialize_autoreview_helper",
        lambda workdir: None,
    )
    parent = _parent(repo)
    parent._origin_work_item_id = "discord-work"
    parent._origin_work_item_generation = 4
    parent._origin_work_item_attempt_id = "gateway-epoch:4"
    parent._origin_work_item_attempt_order = 11

    handle = json.loads(
        cwt.delegate_coding_task(
            task="update the parser",
            model_tier="trivial",
            scope_paths=["src"],
            background=True,
            parent_agent=parent,
        )
    )
    event = _drain_background_completion()

    assert handle["success"] is True
    names = [call[0] for call in calls]
    assert names.index("register") < names.index("running") < names.index("model_start")
    registration = next(call for call in calls if call[0] == "register")[2]
    assert registration["delegation_id"] == handle["delegation_id"]
    assert registration["owner_pid"] == os.getpid()
    assert registration["process_epoch"] == "gateway-epoch"
    assert registration["scope_paths"] == ["src"]
    assert registration["recovery"]["task"] == "update the parser"
    assert registration["recovery"]["policy"] == "resume_or_relaunch"
    assert registration["recovery"]["scope_paths"] == ["src"]
    checkpoint = next(call for call in calls if call[0] == "checkpoint")[2]["updates"]
    assert checkpoint["worktree"] == str(repo)
    assert len(checkpoint["base_sha"]) == 40
    assert checkpoint["model_tier"] == "trivial"
    assert checkpoint["git_top_level"] == str(repo)
    assert checkpoint["git_common_dir"].endswith("/.git")
    assert event["delegation_id"] == handle["delegation_id"]
    completion = next(call for call in calls if call[0] == "complete")[2]
    head_sha = completion["evidence"]["head_sha"]
    assert len(head_sha) == 40
    assert set(head_sha) <= set("0123456789abcdef")
    assert event["result"]["head_sha"] == head_sha
    _reset_background_state()


def test_runtime_recovery_checkpoint_redacts_nested_strings(monkeypatch):
    captured = {}

    class Ledger:
        def update_required_async_dispatch_recovery(self, _work_id, **kwargs):
            captured.update(kwargs["updates"])
            return {"dispatches": {kwargs["delegation_id"]: {"state": "running"}}}

    monkeypatch.setattr(ad, "_required_async_ledger", lambda: Ledger())
    monkeypatch.setattr(
        "agent.redact.redact_sensitive_text",
        lambda value, force=False: str(value).replace("SECRET", "[REDACTED]"),
    )
    startup = cwt._BackgroundCodingStartup(
        task="task",
        context_pack={},
        delegation_id="deleg-redact",
        origin_work_item_id="work-redact",
        origin_run_generation=1,
        origin_attempt_id="epoch:1",
        origin_attempt_order=1,
        origin_owner_pid=123,
        origin_process_epoch="epoch",
    )

    assert startup.persist_recovery(
        force=True,
        plan_text="use SECRET",
        relevant_files=[{"note": "SECRET nested"}],
    )
    assert captured["plan_text"] == "use [REDACTED]"
    assert captured["relevant_files"] == [{"note": "[REDACTED] nested"}]


def test_recovered_startup_rechecks_git_identity_after_release(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_worktree(repo)
    top, common, head = cwt._git_workspace_identity(str(repo))
    startup = cwt._BackgroundCodingStartup(
        task="task",
        context_pack={},
        recovery_launch=True,
        base_sha=head,
        git_top_level=top,
        git_common_dir=common,
    )
    result = {}

    def ready_worker():
        result["accepted"] = startup.mark_ready(
            worker_cwd=str(repo),
            model_tier=None,
            scope_paths=["src"],
            backend="codex",
            worker_run={},
        )

    thread = threading.Thread(target=ready_worker)
    thread.start()
    assert startup.ready.wait(5)
    (repo / "changed.txt").write_text("changed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "changed.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "change head"], check=True)
    startup.release.set()
    thread.join(5)

    assert result["accepted"] is False
    assert "changed before release" in startup.cancel_reason
    assert startup.model_tier == "default"
    assert startup.recovery_model_tier == ""


def test_failed_parallel_preflight_preserves_isolated_worktree(tmp_path):
    worker = tmp_path / "worker"
    worker.mkdir()
    startup = cwt._BackgroundCodingStartup(
        task="task",
        context_pack={},
        parallel_group={"group_id": "group-1", "base_cwd": str(tmp_path)},
        worker_cwd=str(worker),
        cancel_reason="identity changed",
    )
    payload = {"success": False, "error": startup.cancel_reason}
    resolved = str(worker.resolve())
    with cwt._BACKGROUND_PARALLEL_WORKERS_GUARD:
        cwt._BACKGROUND_PARALLEL_WORKERS.add(resolved)

    cwt._preserve_failed_background_parallel_result(payload, startup)

    assert worker.exists()
    assert payload["parallel_merge"]["merged"] is False
    assert payload["parallel_merge"]["worktree_kept"] is True
    assert resolved not in cwt._BACKGROUND_PARALLEL_WORKERS


def test_required_background_preflight_failure_is_durably_terminalized(
    monkeypatch,
    tmp_path,
):
    _reset_background_state()
    calls = []

    class Ledger:
        def register_required_async_dispatch(self, work_id, **kwargs):
            calls.append(("register", work_id, kwargs))
            return {"dispatches": {kwargs["delegation_id"]: {"state": "registered"}}}

        def record_required_async_completion(self, work_id, **kwargs):
            calls.append(("complete", work_id, kwargs))
            return {
                "dispatches": {
                    kwargs["delegation_id"]: {"state": "terminal", "success": False}
                }
            }

    cfg = copy.deepcopy(DEFAULT_CONFIG)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    monkeypatch.setattr(ad, "_required_async_ledger", lambda: Ledger())
    parent = _parent(tmp_path)
    parent._origin_work_item_id = "discord-work"
    parent._origin_work_item_generation = 4
    parent._origin_work_item_attempt_id = "gateway-epoch:4"
    parent._origin_work_item_attempt_order = 11

    result = json.loads(
        cwt.delegate_coding_task(
            task="use missing cwd",
            cwd=str(tmp_path / "missing" / "repo"),
            scope_paths=["src"],
            background=True,
            parent_agent=parent,
        )
    )

    assert "cwd does not exist" in result["error"]
    assert [call[0] for call in calls] == ["register", "complete"]
    completion = calls[-1][2]
    assert completion["success"] is False
    assert completion["status"] == "preflight_failed"
    assert process_registry.completion_queue.empty()
    _reset_background_state()


@pytest.mark.parametrize("scope_paths", [None, []])
def test_durable_background_worker_rejects_omitted_scopes_before_dispatch(
    monkeypatch,
    tmp_path,
    scope_paths,
):
    _reset_background_state()
    parent = _parent(tmp_path)
    parent._origin_work_item_id = "discord-work"
    parent._origin_work_item_generation = 4
    parent._origin_work_item_attempt_id = "gateway-epoch:4"
    parent._origin_work_item_attempt_order = 11
    monkeypatch.setattr(
        ad,
        "_required_async_ledger",
        lambda: (_ for _ in ()).throw(AssertionError("dispatch must not register")),
    )
    FakeSession.instances = []

    result = json.loads(
        cwt.delegate_coding_task(
            task="mutate without a declared scope",
            scope_paths=scope_paths,
            background=True,
            parent_agent=parent,
        )
    )

    assert "requires non-empty scope_paths" in result["error"]
    assert FakeSession.instances == []
    assert ad.active_count() == 0
    _reset_background_state()


def test_background_dispatch_returns_handle_and_records_worker_run(monkeypatch, tmp_path):
    _reset_background_state()
    gate = threading.Event()
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_worktree(repo)

    class BlockingSession(FakeSession):
        def run_turn(self, **kwargs):
            assert gate.wait(5)
            return super().run_turn(**kwargs)

    cfg = copy.deepcopy(DEFAULT_CONFIG)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        BlockingSession,
    )
    monkeypatch.setattr(
        "hermes_cli.worker_autoreview.materialize_autoreview_helper",
        lambda workdir: None,
    )
    parent = _parent(repo)

    started = time.monotonic()
    handle = json.loads(
        cwt.delegate_coding_task(
            task="update the parser",
            model_tier="trivial",
            scope_paths=["src"],
            background=True,
            parent_agent=parent,
        )
    )

    assert time.monotonic() - started < 2
    assert handle == {
        "success": True,
        "background": True,
        "delegation_id": handle["delegation_id"],
        "worker_cwd": str(repo),
        "model_tier": "trivial",
        "scope_paths": ["src"],
        "note": (
            "worker running; its result is attached to the originating attempt "
            "and will be included in that attempt's single terminal response"
        ),
    }
    assert handle["delegation_id"].startswith("deleg_")
    assert parent.turn_worker_runs[0]["background"] is True
    assert parent.turn_worker_runs[0]["model_tier"] == "trivial"
    assert process_registry.completion_queue.empty()

    gate.set()
    event = _drain_background_completion()
    assert event["kind"] == "coding_worker"
    assert event["worker_run"]["background"] is True
    assert event["result"]["scope_check"] == {
        "scope_paths": ["src"],
        "changed_files": [],
        "out_of_scope_files": [],
        "clean": True,
    }
    _reset_background_state()


def test_background_disabled_and_capacity_errors(monkeypatch, tmp_path):
    _reset_background_state()
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["coding_worker"]["background"]["enabled"] = False
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    disabled = json.loads(
        cwt.delegate_coding_task(
            task="disabled",
            background=True,
            parent_agent=_parent(tmp_path),
        )
    )
    assert "coding_worker.background.enabled=false" in disabled["error"]

    cfg["coding_worker"]["background"] = {"enabled": True, "max_concurrent": 1}
    gate = threading.Event()

    class BlockingSession(FakeSession):
        def run_turn(self, **kwargs):
            assert gate.wait(5)
            return super().run_turn(**kwargs)

    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        BlockingSession,
    )
    monkeypatch.setattr(
        "hermes_cli.worker_autoreview.materialize_autoreview_helper",
        lambda workdir: None,
    )
    first = json.loads(
        cwt.delegate_coding_task(
            task="first",
            background=True,
            parent_agent=_parent(tmp_path),
        )
    )
    assert first["success"] is True
    over_limit = json.loads(
        cwt.delegate_coding_task(
            task="second",
            background=True,
            parent_agent=_parent(tmp_path),
        )
    )
    assert "capacity reached" in over_limit["error"].lower()
    assert "background=false" in over_limit["error"]
    gate.set()
    _drain_background_completion()
    _reset_background_state()


def test_background_preflight_failure_is_synchronous(monkeypatch, tmp_path):
    _reset_background_state()
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)

    result = json.loads(
        cwt.delegate_coding_task(
            task="use missing cwd",
            cwd=str(tmp_path / "missing" / "repo"),
            background=True,
            parent_agent=_parent(tmp_path),
        )
    )

    assert "cwd does not exist" in result["error"]
    assert process_registry.completion_queue.empty()
    assert ad.active_count(kind="coding_worker") == 0
    _reset_background_state()


def test_background_refuses_cron_and_kanban_contexts(monkeypatch, tmp_path):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    cron_parent = _parent(tmp_path)
    cron_parent.platform = "cron"

    cron_result = json.loads(
        cwt.delegate_coding_task(
            task="cron background",
            background=True,
            parent_agent=cron_parent,
        )
    )
    assert "unavailable in cron sessions" in cron_result["error"]

    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-1")
    kanban_result = json.loads(
        cwt.delegate_coding_task(
            task="kanban background",
            background=True,
            parent_agent=_parent(tmp_path),
        )
    )
    assert "unavailable in Kanban worker sessions" in kanban_result["error"]


def test_background_cron_gate_resets_for_later_gateway_context(monkeypatch, tmp_path):
    from gateway.session_context import reset_cron_execution, set_cron_execution

    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    parent = _parent(tmp_path)
    parent.platform = "discord"

    token = set_cron_execution()
    try:
        assert "unavailable in cron sessions" in cwt._background_context_error(parent)
    finally:
        reset_cron_execution(token)

    assert cwt._background_context_error(parent) == ""


def test_background_cron_gate_is_concurrent_context_local(monkeypatch, tmp_path):
    from gateway.session_context import reset_cron_execution, set_cron_execution

    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    barrier = threading.Barrier(2, timeout=5)

    def check_cron():
        parent = _parent(tmp_path)
        parent.platform = "discord"
        token = set_cron_execution()
        try:
            barrier.wait()
            return cwt._background_context_error(parent)
        finally:
            reset_cron_execution(token)

    def check_gateway():
        parent = _parent(tmp_path)
        parent.platform = "telegram"
        barrier.wait()
        return cwt._background_context_error(parent)

    with ThreadPoolExecutor(max_workers=2) as pool:
        cron_future = pool.submit(check_cron)
        gateway_future = pool.submit(check_gateway)

    assert "unavailable in cron sessions" in cron_future.result()
    assert gateway_future.result() == ""


def test_background_refuses_session_without_async_delivery(monkeypatch, tmp_path):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    monkeypatch.setattr(
        "gateway.session_context.async_delivery_supported",
        lambda: False,
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="stateless background",
            background=True,
            parent_agent=_parent(tmp_path),
        )
    )

    assert "cannot receive a completion turn" in result["error"]


def test_background_parallel_completion_merges_and_cleans_worktree(monkeypatch, tmp_path):
    _reset_background_state()
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_worktree(repo)
    gate = threading.Event()
    edited = threading.Event()

    class EditingSession(FakeSession):
        def run_turn(self, **kwargs):
            worker_cwd = Path(self.kwargs["cwd"])
            (worker_cwd / "src" / "app.py").write_text(
                "background value\n",
                encoding="utf-8",
            )
            edited.set()
            assert gate.wait(5)
            return super().run_turn(**kwargs)

    cfg = copy.deepcopy(DEFAULT_CONFIG)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        EditingSession,
    )
    monkeypatch.setattr(
        "hermes_cli.worker_autoreview.materialize_autoreview_helper",
        lambda workdir: None,
    )

    handle = json.loads(
        cwt.delegate_coding_task(
            task="edit src",
            scope_paths=["src"],
            background=True,
            parent_agent=_parent(repo),
            _parallel_group={"group_id": "async-clean", "base_cwd": str(repo)},
        )
    )
    worker_cwd = Path(handle["worker_cwd"])
    assert worker_cwd.exists()
    assert cwt.merge_parallel_worker_result(
        str(repo),
        str(worker_cwd),
        "async-clean",
    )["merge_pending"] is True
    assert edited.wait(5)
    gate.set()

    event = _drain_background_completion()
    assert event["result"]["scope_check"]["clean"] is True
    expected_merge = {
        "group_id": "async-clean",
        "worker_cwd": str(worker_cwd),
        "merged": True,
        "merge_conflicts": [],
        "worktree_kept": False,
    }
    assert event["result"]["parallel_merge"] == expected_merge
    assert cwt.merge_parallel_worker_result(
        str(repo),
        str(worker_cwd),
        "async-clean",
    ) == expected_merge
    assert (repo / "src" / "app.py").read_text(encoding="utf-8") == "background value\n"
    assert not worker_cwd.exists()
    _reset_background_state()


def test_background_parallel_conflict_keeps_worker_worktree(monkeypatch, tmp_path):
    _reset_background_state()
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_worktree(repo)
    gate = threading.Event()
    edited = threading.Event()

    class EditingSession(FakeSession):
        def run_turn(self, **kwargs):
            worker_cwd = Path(self.kwargs["cwd"])
            (worker_cwd / "src" / "app.py").write_text("worker value\n", encoding="utf-8")
            edited.set()
            assert gate.wait(5)
            return super().run_turn(**kwargs)

    cfg = copy.deepcopy(DEFAULT_CONFIG)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        EditingSession,
    )
    monkeypatch.setattr(
        "hermes_cli.worker_autoreview.materialize_autoreview_helper",
        lambda workdir: None,
    )
    handle = json.loads(
        cwt.delegate_coding_task(
            task="edit conflicting src",
            scope_paths=["src"],
            background=True,
            parent_agent=_parent(repo),
            _parallel_group={"group_id": "async-conflict", "base_cwd": str(repo)},
        )
    )
    worker_cwd = Path(handle["worker_cwd"])
    assert edited.wait(5)
    (repo / "src" / "app.py").write_text("base value\n", encoding="utf-8")
    gate.set()

    event = _drain_background_completion()
    merge = event["result"]["parallel_merge"]
    assert merge["merged"] is False
    assert merge["recovery_required"] is True
    assert merge["worktree_kept"] is True
    assert worker_cwd.exists()

    subprocess.run(
        ["git", "worktree", "remove", "--force", str(worker_cwd)],
        cwd=repo,
        check=True,
    )
    branch = subprocess.check_output(
        ["git", "branch", "--list", "hermes-parallel/async-conflict-*"],
        cwd=repo,
        text=True,
    ).strip()
    if branch:
        subprocess.run(["git", "branch", "-D", branch], cwd=repo, check=True)
    _reset_background_state()


def test_background_parallel_fable_marker_uses_normal_preflight(tmp_path):
    result = json.loads(
        cwt.delegate_coding_task(
            task="parallel fable",
            background=True,
            parent_agent=_fable_parent(tmp_path),
            _parallel_group={"group_id": "fable-bg", "base_cwd": str(tmp_path)},
        )
    )

    assert "do not yet support _parallel_group" not in result["error"]
    assert "not a git repository" in result["error"]


def test_scope_check_reports_in_scope_changes_as_clean(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_worktree(repo)
    (repo / "src" / "app.py").write_text("value = 2\n", encoding="utf-8")
    FakeSession.instances = []
    FakeSession.results = []
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )
    monkeypatch.setattr(
        "hermes_cli.worker_autoreview.materialize_autoreview_helper",
        lambda workdir: None,
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="update the app",
            scope_paths=["src"],
            parent_agent=_parent(repo),
        )
    )

    assert result["scope_check"] == {
        "scope_paths": ["src"],
        "changed_files": ["src/app.py"],
        "out_of_scope_files": [],
        "clean": True,
    }
    prompt = FakeSession.instances[0].run_calls[0]["user_input"]
    assert "You may only modify files under these workdir-relative prefixes" in prompt
    assert "- `src`" in prompt


def test_scope_check_lists_out_of_scope_changes(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_worktree(repo)
    (repo / "src" / "app.py").write_text("value = 2\n", encoding="utf-8")
    (repo / "README.md").write_text("changed outside scope\n", encoding="utf-8")
    FakeSession.instances = []
    FakeSession.results = []
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )
    monkeypatch.setattr(
        "hermes_cli.worker_autoreview.materialize_autoreview_helper",
        lambda workdir: None,
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="update the app",
            scope_paths=["src"],
            parent_agent=_parent(repo),
        )
    )

    assert result["scope_check"] == {
        "scope_paths": ["src"],
        "changed_files": ["README.md", "src/app.py"],
        "out_of_scope_files": ["README.md"],
        "clean": False,
    }


def test_parallel_group_returns_pending_isolated_worktree_without_merging(
    monkeypatch,
    tmp_path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_worktree(repo)
    base_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    (repo / "README.md").write_text("pre-existing base edit\n", encoding="utf-8")
    seen = {}

    class EditingSession(FakeSession):
        def run_turn(self, **kwargs):
            worker_cwd = Path(self.kwargs["cwd"])
            seen["cwd"] = worker_cwd
            seen["head"] = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=worker_cwd, text=True
            ).strip()
            seen["readme"] = (worker_cwd / "README.md").read_text(encoding="utf-8")
            (worker_cwd / "src" / "app.py").write_text("value = 2\n", encoding="utf-8")
            (worker_cwd / "src" / "new.py").write_text("NEW = True\n", encoding="utf-8")
            return super().run_turn(**kwargs)

    cfg = copy.deepcopy(DEFAULT_CONFIG)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        EditingSession,
    )
    monkeypatch.setattr(
        "hermes_cli.worker_autoreview.materialize_autoreview_helper",
        lambda workdir: None,
    )
    merge_back = cwt.merge_parallel_worker_result
    merge_mock = MagicMock(side_effect=AssertionError("wrapper must not merge"))
    monkeypatch.setattr(cwt, "merge_parallel_worker_result", merge_mock)

    result = json.loads(
        cwt.delegate_coding_task(
            task="update only src",
            scope_paths=["src"],
            parent_agent=_parent(repo),
            _parallel_group={"group_id": "batch-1", "base_cwd": str(repo)},
        )
    )

    assert result["success"] is True
    assert result["parallel"] == {
        "group_id": "batch-1",
        "worker_cwd": str(seen["cwd"]),
        "merged": False,
        "merge_pending": True,
        "merge_conflicts": [],
        "worktree_kept": True,
    }
    assert result["scope_check"] == {
        "scope_paths": ["src"],
        "changed_files": ["src/app.py", "src/new.py"],
        "out_of_scope_files": [],
        "clean": True,
    }
    assert seen["cwd"] != repo
    assert "-pw-batch-1-" in seen["cwd"].name
    assert seen["head"] == base_head
    assert seen["readme"] == "baseline\n"
    assert seen["cwd"].exists()
    assert (seen["cwd"] / "src" / "app.py").read_text(encoding="utf-8") == "value = 2\n"
    assert (seen["cwd"] / "src" / "new.py").read_text(encoding="utf-8") == "NEW = True\n"
    assert (repo / "README.md").read_text(encoding="utf-8") == "pre-existing base edit\n"
    assert (repo / "src" / "app.py").read_text(encoding="utf-8") == "value = 1\n"
    assert not (repo / "src" / "new.py").exists()
    merge_mock.assert_not_called()

    merge_result = merge_back(str(repo), str(seen["cwd"]), "batch-1")
    assert merge_result["merged"] is True
    assert not seen["cwd"].exists()


def test_merge_parallel_worker_result_handles_modify_add_delete_and_mode(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_worktree(repo)
    (repo / "delete.txt").write_text("delete me\n", encoding="utf-8")
    subprocess.run(["git", "add", "delete.txt"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "add delete fixture"],
        cwd=repo,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    worker = cwt._provision_parallel_worker(str(repo), "clean-merge")
    worker_cwd = Path(worker.worker_cwd)
    (worker_cwd / "src" / "app.py").write_text("value = 3\n", encoding="utf-8")
    (worker_cwd / "added.txt").write_text("added\n", encoding="utf-8")
    (worker_cwd / "delete.txt").unlink()
    (worker_cwd / "README.md").chmod(0o755)

    result = cwt.merge_parallel_worker_result(
        str(repo),
        str(worker_cwd),
        "clean-merge",
    )

    assert result == {
        "group_id": "clean-merge",
        "worker_cwd": str(worker_cwd),
        "merged": True,
        "merge_conflicts": [],
        "worktree_kept": False,
    }
    assert (repo / "src" / "app.py").read_text(encoding="utf-8") == "value = 3\n"
    assert (repo / "added.txt").read_text(encoding="utf-8") == "added\n"
    assert not (repo / "delete.txt").exists()
    assert stat.S_IMODE((repo / "README.md").stat().st_mode) & 0o111 == 0o111
    assert not Path(worker.worker_root).exists()
    assert subprocess.check_output(
        ["git", "branch", "--list", worker.branch], cwd=repo, text=True
    ).strip() == ""


def test_merge_parallel_worker_conflict_leaves_base_untouched_and_keeps_worktree(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_worktree(repo)
    worker = cwt._provision_parallel_worker(str(repo), "conflict-merge")
    worker_cwd = Path(worker.worker_cwd)
    (worker_cwd / "src" / "app.py").write_text("worker value\n", encoding="utf-8")
    (repo / "src" / "app.py").write_text("base value\n", encoding="utf-8")
    before_content = (repo / "src" / "app.py").read_bytes()
    before_status = subprocess.check_output(
        ["git", "status", "--porcelain=v1"], cwd=repo
    )

    result = cwt.merge_parallel_worker_result(
        str(repo),
        str(worker_cwd),
        "conflict-merge",
    )

    assert result["merged"] is False
    assert result["merge_conflicts"] == ["src/app.py"]
    assert result["worktree_kept"] is True
    assert result["recovery_required"] is True
    assert str(worker_cwd) in result["next_action"]
    assert (repo / "src" / "app.py").read_bytes() == before_content
    assert subprocess.check_output(
        ["git", "status", "--porcelain=v1"], cwd=repo
    ) == before_status
    assert worker_cwd.exists()

    subprocess.run(
        ["git", "worktree", "remove", "--force", worker.worker_root],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "branch", "-D", worker.branch], cwd=repo, check=True)


def test_parallel_merge_lock_serializes_same_group(monkeypatch, tmp_path):
    first_entered = threading.Event()
    release_first = threading.Event()
    second_submitted = threading.Event()
    order = []

    def fake_merge(base_cwd, worker_cwd, group_id):
        name = Path(worker_cwd).name
        order.append(f"start:{name}")
        if name == "worker-1":
            first_entered.set()
            assert release_first.wait(5)
        order.append(f"end:{name}")
        return {
            "group_id": group_id,
            "worker_cwd": worker_cwd,
            "merged": True,
            "merge_conflicts": [],
            "worktree_kept": False,
        }

    monkeypatch.setattr(cwt, "_merge_parallel_worker_result_locked", fake_merge)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            cwt.merge_parallel_worker_result,
            str(tmp_path),
            str(tmp_path / "worker-1"),
            "serialized",
        )
        assert first_entered.wait(5)

        def run_second():
            second_submitted.set()
            return cwt.merge_parallel_worker_result(
                str(tmp_path),
                str(tmp_path / "worker-2"),
                "serialized",
            )

        second = executor.submit(run_second)
        assert second_submitted.wait(5)
        assert order == ["start:worker-1"]
        release_first.set()
        first.result(timeout=5)
        second.result(timeout=5)

    assert order == [
        "start:worker-1",
        "end:worker-1",
        "start:worker-2",
        "end:worker-2",
    ]


def test_parallel_disabled_falls_back_to_in_place_execution(monkeypatch, tmp_path):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["coding_worker"]["parallel"]["enabled"] = False
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )
    monkeypatch.setattr(
        "hermes_cli.worker_autoreview.materialize_autoreview_helper",
        lambda workdir: None,
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="run without isolation",
            parent_agent=_parent(tmp_path),
            _parallel_group={"group_id": "disabled", "base_cwd": str(tmp_path)},
        )
    )

    assert result["success"] is True
    assert result["cwd"] == str(tmp_path)
    assert result["parallel"] == {"disabled": True}
    assert not list(tmp_path.parent.glob(f"{tmp_path.name}-pw-*"))


def test_turn_worker_runs_append_is_thread_safe():
    parent = SimpleNamespace()
    barrier = threading.Barrier(33)

    def start(index):
        barrier.wait()
        return cwt._start_worker_run(
            parent,
            backend="codex",
            model=f"model-{index}",
            reasoning="medium",
            model_tier=None,
        )

    with ThreadPoolExecutor(max_workers=32) as executor:
        futures = [executor.submit(start, index) for index in range(32)]
        barrier.wait()
        records = [future.result(timeout=5) for future in futures]

    assert len(parent.turn_worker_runs) == 32
    assert {record["model"] for record in parent.turn_worker_runs} == {
        f"model-{index}" for index in range(32)
    }
    assert all(record in parent.turn_worker_runs for record in records)


def test_delegate_repairs_missing_task_from_worker_context(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )

    result = json.loads(
        cwt.delegate_coding_task(
            context=(
                "User request: fix the parser startup regression.\n\n"
                "Task:\n"
                "Fix the parser startup regression and run focused tests.\n\n"
                "Verification:\n"
                "Report changed files and checks."
            ),
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True
    assert result["task_inferred_from_context"] is True
    prompt = FakeSession.instances[0].run_calls[0]["user_input"]
    assert "Hermes tool-call repair: delegate_coding_task was invoked without" in prompt
    assert "Task:\nFix the parser startup regression and run focused tests." in prompt
    task_section = prompt.split("Task:\n", 1)[1].split("\n\nContext from Hermes:", 1)[0]
    assert "Verification:" not in task_section


def test_delegate_falls_back_to_workspace_parent_for_missing_cwd(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    workspace = tmp_path / "workspaces"
    workspace.mkdir()
    requested = workspace / "reserve-index-dtf"
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="Fix the missed PR diff cleanup.",
            context="Locate the checkout or clone it if absent.",
            cwd=str(requested),
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True
    assert result["cwd"] == str(workspace.resolve())
    assert result["cwd_fallback"] == {
        "requested_cwd": str(requested),
        "fallback_cwd": str(workspace.resolve()),
        "reason": "requested cwd did not exist",
    }
    assert FakeSession.instances[0].kwargs["cwd"] == str(workspace.resolve())
    prompt = FakeSession.instances[0].run_calls[0]["user_input"]
    assert "Hermes cwd repair: the requested worker cwd did not exist" in prompt
    assert "First locate an existing checkout or clone/create the intended repository path" in prompt
    assert "Autoreview helper materialization was deferred" in prompt
    assert "Repository context loaded by Hermes" not in prompt
    assert not (workspace / ".agents" / "skills" / "autoreview").exists()


def test_delegate_preserves_json_route_decision_with_missing_cwd_fallback(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    workspace = tmp_path / "workspaces"
    workspace.mkdir()
    requested = workspace / "missing-command-center-checkout"
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["coding_worker"]["backend"] = "codex"
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="Implement Command Center card footer visual polish.",
            context="Requested checkout is absent; start from workspace parent.",
            cwd=str(requested),
            route_decision=json.dumps(
                {
                    "route": "ui_visual_specialist",
                    "source": "orchestrator",
                    "confidence": 0.97,
                    "rationale": "Command Center UI task",
                }
            ),
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True
    assert result["cwd"] == str(workspace.resolve())
    assert result["cwd_fallback"] == {
        "requested_cwd": str(requested),
        "fallback_cwd": str(workspace.resolve()),
        "reason": "requested cwd did not exist",
    }
    route = result["ui_work_route"]
    assert route["matched"] is True
    assert route["reason"] == "orchestrator route selected ui visual specialist"
    assert route["route_decision"] == "ui_visual_specialist"
    assert route["route_decision_source"] == "orchestrator"
    assert route["route_decision_confidence"] == 0.97
    assert route["route_decision_rationale"] == "Command Center UI task"
    assert route["selected_route"] == "ui_visual_specialist"
    assert route["selected_provider"] == ""
    assert route["selected_model"] == ""
    assert result["backend"] == "codex"
    assert FakeSession.instances


def test_ui_specialist_route_uses_normal_codex_backend_and_skills(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["coding_worker"]["backend"] = "codex"
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )
    parent = _parent(tmp_path)

    result = json.loads(
        cwt.delegate_coding_task(
            task="Polish the Command Center card spacing.",
            route_decision={"route": "ui_visual_specialist"},
            parent_agent=parent,
        )
    )

    assert result["success"] is True
    assert result["backend"] == "codex"
    assert len(FakeSession.instances) == 1
    prompt = FakeSession.instances[0].run_calls[0]["user_input"]
    assert "UI specialist skill loading" in prompt
    assert "`taste-skill`" in prompt
    assert "`claude-design`" in prompt
    assert "`popular-web-designs`" in prompt
    assert parent.turn_worker_runs == [
        {
            "backend": "codex",
            "model": "gpt-5.6-sol",
            "reasoning": "low",
            "model_tier": None,
        },
    ]


def test_automatic_visual_route_injects_opus_advisor_guidance(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["coding_worker"]["backend"] = "codex"
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )
    calls = []

    def fake_advisor(**kwargs):
        calls.append(kwargs)
        return (
            "Use a strong information hierarchy, restrained color, and compact responsive spacing.",
            {
                "advisor_invoked": True,
                "advisor_status": "completed",
                "advisor_model": "claude-opus-5",
                "advisor_cached": False,
            },
        )

    monkeypatch.setattr(cwt, "_run_ui_visual_advisor", fake_advisor)

    result = json.loads(
        cwt.delegate_coding_task(
            task="Polish the responsive dashboard card spacing and typography.",
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True
    assert len(calls) == 1
    assert result["ui_work_route"]["selected_route"] == "ui_visual_specialist"
    assert result["ui_work_route"]["route_decision_source"] == "deterministic_explicit_visual"
    assert result["ui_work_route"]["advisor_model"] == "claude-opus-5"
    prompt = FakeSession.instances[0].run_calls[0]["user_input"]
    assert "Opus visual advisor guidance" in prompt
    assert "strong information hierarchy" in prompt


def test_ui_visual_advisor_helper_caches_same_turn_result(monkeypatch, tmp_path):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["ui_work"]["route_delegate_task"] = False
    parent = _parent(tmp_path)
    parent._current_turn_id = "turn-visual-1"
    calls = []
    monkeypatch.setattr(
        "hermes_cli.opus_planner._anthropic_budget_preflight_error",
        lambda: "",
    )

    def fake_delegate_task(**kwargs):
        calls.append(kwargs)
        return json.dumps(
            {
                "results": [
                    {
                        "status": "completed",
                        "summary": "Keep the layout calm and align controls to one grid.",
                        "model": "claude-opus-5",
                        "handoff": {"handoff_id": "handoff_visual_1"},
                    }
                ]
            }
        )

    monkeypatch.setattr("tools.delegate_tool.delegate_task", fake_delegate_task)
    route = SimpleNamespace(
        selected_route="ui_visual_specialist",
        launch_worker=True,
    )
    args = dict(
        loaded_config=cfg,
        ui_route=route,
        task="Polish responsive dashboard spacing.",
        context="Keep existing components.",
        workdir=str(tmp_path),
        relevant_files=[{"path": "src/App.tsx", "note": "dashboard"}],
        approach="Reuse the grid.",
        constraints="Do not change APIs.",
        parent_agent=parent,
    )

    first_guidance, first_metadata = _REAL_UI_VISUAL_ADVISOR(**args)
    second_guidance, second_metadata = _REAL_UI_VISUAL_ADVISOR(**args)

    assert len(calls) == 1
    assert calls[0]["purpose"] == "visual_advisor"
    assert calls[0]["read_only"] is True
    assert first_guidance == second_guidance
    assert first_metadata["advisor_status"] == "completed"
    assert second_metadata["advisor_cached"] is True


def test_ui_visual_advisor_failure_is_fail_open(monkeypatch, tmp_path):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    parent = _parent(tmp_path)
    monkeypatch.setattr(
        "hermes_cli.opus_planner._anthropic_budget_preflight_error",
        lambda: "",
    )
    monkeypatch.setattr(
        "tools.delegate_tool.delegate_task",
        lambda **kwargs: json.dumps(
            {
                "results": [
                    {
                        "status": "failed",
                        "exit_reason": "provider_failure",
                        "model": "claude-opus-5",
                    }
                ]
            }
        ),
    )

    guidance, metadata = _REAL_UI_VISUAL_ADVISOR(
        loaded_config=cfg,
        ui_route=SimpleNamespace(
            selected_route="ui_visual_specialist",
            launch_worker=True,
        ),
        task="Polish responsive dashboard spacing.",
        context="",
        workdir=str(tmp_path),
        relevant_files=None,
        approach=None,
        constraints=None,
        parent_agent=parent,
    )

    assert guidance == ""
    assert metadata["advisor_invoked"] is True
    assert metadata["advisor_status"] == "failed"
    assert metadata["advisor_failure_class"] == "provider_failure"


def test_ui_visual_advisor_skips_known_opus_budget_exhaustion(monkeypatch, tmp_path):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    parent = _parent(tmp_path)
    monkeypatch.setattr(
        "hermes_cli.opus_planner._anthropic_budget_preflight_error",
        lambda: "Opus extra usage exhausted",
    )
    delegate = MagicMock()
    monkeypatch.setattr("tools.delegate_tool.delegate_task", delegate)

    guidance, metadata = _REAL_UI_VISUAL_ADVISOR(
        loaded_config=cfg,
        ui_route=SimpleNamespace(
            selected_route="ui_visual_specialist",
            launch_worker=True,
        ),
        task="Polish responsive dashboard spacing.",
        context="",
        workdir=str(tmp_path),
        relevant_files=None,
        approach=None,
        constraints=None,
        parent_agent=parent,
    )

    assert guidance == ""
    assert metadata["advisor_status"] == "skipped"
    assert metadata["advisor_failure_class"] == "opus_budget_exhausted"
    delegate.assert_not_called()


def test_ui_specialist_route_uses_normal_opencode_backend(monkeypatch, tmp_path):
    from agent import opencode_worker as ow

    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["coding_worker"]["backend"] = "opencode"
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    monkeypatch.setattr(
        ow,
        "load_coding_worker_backend",
        lambda config=None, worker_config=None: ow.BACKEND_OPENCODE,
    )
    seen = {}

    def fake_run(prompt, workspace, **kwargs):
        seen["prompt"] = prompt
        seen.update(kwargs)
        return SimpleNamespace(
            final_text="Changed src/app.py and ran npm test.",
            error=None,
            interrupted=False,
            agents=["build"],
            plan_text="",
            thread_id="ses-build",
            turn_id="ses-build",
            tool_iterations=1,
        )

    monkeypatch.setattr(ow, "run_opencode_task", fake_run)

    result = json.loads(
        cwt.delegate_coding_task(
            task="Polish the Command Center card spacing.",
            route_decision={"route": "ui_visual_specialist"},
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True
    assert result["backend"] == "opencode"
    route = result["ui_work_route"]
    assert route["matched"] is True
    assert route["backend"] == "opencode"
    assert route["selected_provider"] == ""
    assert route["selected_model"] == ""
    assert route["actual_backend"] == "opencode"
    assert route["actual_model"]
    assert route["actual_reasoning_effort"]
    assert "UI specialist skill loading" in seen["prompt"]
    assert "worker_config" not in seen


def test_default_opencode_route_keeps_openrouter_key_scrubbed(monkeypatch, tmp_path):
    from agent import opencode_worker as ow

    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["coding_worker"]["backend"] = "opencode"
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    monkeypatch.setattr(
        ow,
        "load_coding_worker_backend",
        lambda config=None, worker_config=None: ow.BACKEND_OPENCODE,
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-secret")
    seen = {}

    def fake_run(prompt, workspace, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(
            final_text="Changed src/parser.py and ran pytest.",
            error=None,
            interrupted=False,
            agents=["build"],
            plan_text="",
            thread_id="ses-build",
            turn_id="ses-build",
            tool_iterations=1,
        )

    monkeypatch.setattr(ow, "run_opencode_task", fake_run)

    result = json.loads(
        cwt.delegate_coding_task(
            task="Fix the parser bug.",
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True
    assert result["backend"] == "opencode"
    assert "worker_config" not in seen
    assert "env" not in seen


def test_default_codex_route_keeps_openrouter_key_scrubbed(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-secret")
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="Fix the parser bug.",
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True
    env = FakeSession.instances[0].kwargs["env"]
    assert "OPENROUTER_API_KEY" not in env
    assert "_HERMES_FORCE_OPENROUTER_API_KEY" not in env


def test_codex_route_inherits_selected_provider_env_key(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    codex_home = tmp_path / "host-codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        'model_provider = "cliproxy"\n'
        '[model_providers.cliproxy]\n'
        'name = "CLIProxyAPI"\n'
        'base_url = "http://127.0.0.1:8317/v1"\n'
        'wire_api = "responses"\n'
        'env_key = "CLI_PROXY_API_KEY"\n'
        'requires_openai_auth = false\n',
        encoding="utf-8",
    )
    provider_secret = "test-worker-provider-secret"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("CLI_PROXY_API_KEY", provider_secret)
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="Fix the parser bug.",
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True
    session = FakeSession.instances[0]
    assert session.kwargs["env"]["CLI_PROXY_API_KEY"] == provider_secret
    assert "_HERMES_FORCE_CLI_PROXY_API_KEY" not in session.kwargs["env"]
    assert session.auth_payload is None
    assert session.config_payload["model_provider"] == "cliproxy"
    provider = session.config_payload["model_providers"]["cliproxy"]
    assert provider["env_key"] == "CLI_PROXY_API_KEY"
    assert provider["requires_openai_auth"] is False
    assert provider_secret not in json.dumps(session.config_payload)


def test_authorized_git_pr_lifecycle_updates_prompt_and_codex_env(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    gh_config = tmp_path / "gh"
    gh_config.mkdir()
    git_config = tmp_path / ".gitconfig"
    git_config.write_text("[user]\n\tname = Test\n\temail = test@example.invalid\n")
    monkeypatch.setenv("GH_CONFIG_DIR", str(gh_config))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(git_config))
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/ssh-agent.sock")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.setenv("GH_TOKEN", "gho_secret")
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="fix and open a PR",
            parent_agent=_parent(tmp_path),
            allow_git_pr_lifecycle=True,
            trusted_allow_git_pr_lifecycle=True,
        )
    )

    assert result["success"] is True
    kwargs = FakeSession.instances[0].kwargs
    assert kwargs["replace_env"] is False
    env = kwargs["env"]
    assert env["HERMES_SESSION_KEY"] == "discord:123"
    assert env["HERMES_CODEX_WORKER_NETWORK_ACCESS"] == "1"
    assert env["HERMES_CODEX_WORKER_WORKSPACE"] == str(tmp_path)
    assert env["GH_CONFIG_DIR"] == str(gh_config)
    assert env["GIT_CONFIG_GLOBAL"] == str(git_config)
    assert env["SSH_AUTH_SOCK"] == "/tmp/ssh-agent.sock"
    assert "GIT_SSH_COMMAND" not in env
    assert "GITHUB_TOKEN" not in env
    assert "GH_TOKEN" not in env
    prompt = FakeSession.instances[0].run_calls[0]["user_input"]
    assert "Git/PR lifecycle is explicitly authorized" in prompt
    assert "open a non-draft PR" in prompt
    assert "Do not create commits or pull requests." not in prompt
    assert "Do not merge PRs" in prompt


def test_fable_parent_uses_normal_worker_without_local_git_finalizer(
    monkeypatch, tmp_path
):
    FakeSession.instances = []
    FakeSession.results = []
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )
    monkeypatch.setattr(
        "agent.transports.codex_app_server.check_codex_binary",
        lambda: (True, "codex ready"),
    )
    monkeypatch.setitem(sys.modules, "hermes_cli.fable_git_finalizer", None)
    parent = _fable_parent(tmp_path)
    parent._fable_git_lifecycle = "merge"

    result = json.loads(cwt.delegate_coding_task(task="land the requested change", parent_agent=parent))

    assert result["success"] is True
    assert "fable_git_lifecycle" not in result
    assert "fable_git_result" not in result
    env = FakeSession.instances[0].kwargs["env"]
    assert "HERMES_CODEX_WORKER_NETWORK_ACCESS" not in env
    assert "HERMES_CODEX_WORKER_WORKSPACE" not in env
    assert "HERMES_CODEX_WORKER_GIT_COMMON_DIR" not in env
    prompt = FakeSession.instances[0].run_calls[0]["user_input"]
    assert "Fable implementation worker" not in prompt
    assert "pre-provisioned mutable checkout" not in prompt
    assert "Trusted Hermes lifecycle recovery" not in prompt
    assert "Git/PR lifecycle is explicitly authorized" not in prompt


def test_background_fable_parent_returns_normal_worker_result(monkeypatch, tmp_path):
    _reset_background_state()
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )
    monkeypatch.setattr(
        "agent.transports.codex_app_server.check_codex_binary",
        lambda: (True, "codex ready"),
    )
    monkeypatch.setattr(
        "hermes_cli.worker_autoreview.materialize_autoreview_helper",
        lambda workdir: None,
    )
    monkeypatch.setitem(sys.modules, "hermes_cli.fable_git_finalizer", None)
    parent = _fable_parent(tmp_path)
    parent._fable_git_lifecycle = "pr"

    handle = json.loads(
        cwt.delegate_coding_task(
            task="open the requested PR",
            background=True,
            parent_agent=parent,
        )
    )
    assert handle["success"] is True
    event = _drain_background_completion()
    assert event["result"]["success"] is True
    assert "fable_git_result" not in event["result"]
    assert "fable_git_lifecycle" not in event["result"]
    _reset_background_state()


def test_authorized_git_pr_lifecycle_preserves_explicit_git_ssh_command(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -F /custom/config")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.setenv("GH_TOKEN", "gho_secret")
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="fix and open a PR",
            parent_agent=_parent(tmp_path),
            allow_git_pr_lifecycle=True,
            trusted_allow_git_pr_lifecycle=True,
        )
    )

    assert result["success"] is True
    env = FakeSession.instances[0].kwargs["env"]
    assert env["GIT_SSH_COMMAND"] == "ssh -F /custom/config"
    assert "GITHUB_TOKEN" not in env
    assert "GH_TOKEN" not in env


def test_authorized_git_pr_lifecycle_bypasses_system_ssh_config_for_ssh_remotes(
    monkeypatch, tmp_path
):
    FakeSession.instances = []
    FakeSession.results = []
    cwt.subprocess.run(["git", "init"], cwd=tmp_path, check=True, stdout=cwt.subprocess.PIPE)
    cwt.subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:sligo-labs/hermes.git"],
        cwd=tmp_path,
        check=True,
    )
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="fix and open a PR",
            parent_agent=_parent(tmp_path),
            allow_git_pr_lifecycle=True,
            trusted_allow_git_pr_lifecycle=True,
        )
    )

    assert result["success"] is True
    env = FakeSession.instances[0].kwargs["env"]
    assert env["GIT_SSH_COMMAND"] == "ssh -F /dev/null"


def test_authorized_git_pr_lifecycle_does_not_set_git_ssh_command_for_https_remotes(
    monkeypatch, tmp_path
):
    FakeSession.instances = []
    FakeSession.results = []
    cwt.subprocess.run(["git", "init"], cwd=tmp_path, check=True, stdout=cwt.subprocess.PIPE)
    cwt.subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/sligo-labs/hermes.git"],
        cwd=tmp_path,
        check=True,
    )
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="fix and open a PR",
            parent_agent=_parent(tmp_path),
            allow_git_pr_lifecycle=True,
            trusted_allow_git_pr_lifecycle=True,
        )
    )

    assert result["success"] is True
    env = FakeSession.instances[0].kwargs["env"]
    assert "GIT_SSH_COMMAND" not in env


def test_untrusted_git_pr_lifecycle_request_stays_local_only(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    monkeypatch.setenv("GH_TOKEN", "gho_secret")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="fix and open a PR",
            parent_agent=_parent(tmp_path),
            allow_git_pr_lifecycle=True,
        )
    )

    assert result["success"] is True
    env = FakeSession.instances[0].kwargs["env"]
    assert "HERMES_CODEX_WORKER_NETWORK_ACCESS" not in env
    assert "HERMES_CODEX_WORKER_WORKSPACE" not in env
    assert "GH_TOKEN" not in env
    assert "GITHUB_TOKEN" not in env
    prompt = FakeSession.instances[0].run_calls[0]["user_input"]
    assert "Do not create commits or pull requests." in prompt
    assert "Git/PR lifecycle is explicitly authorized" not in prompt


def test_registry_ignores_model_supplied_git_pr_lifecycle_authorization(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )

    entry = cwt.registry.get_entry("delegate_coding_task")
    assert entry is not None
    result = json.loads(
        entry.handler(
            {
                "task": "fix and open a PR",
                "allow_git_pr_lifecycle": True,
            },
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True
    env = FakeSession.instances[0].kwargs["env"]
    assert "HERMES_CODEX_WORKER_NETWORK_ACCESS" not in env
    prompt = FakeSession.instances[0].run_calls[0]["user_input"]
    assert "Do not create commits or pull requests." in prompt


def test_registry_forwards_orchestrator_worker_inputs(monkeypatch, tmp_path):
    captured = {}

    def fake_delegate_coding_task(**kwargs):
        captured.update(kwargs)
        return json.dumps({"success": True})

    monkeypatch.setattr(cwt, "delegate_coding_task", fake_delegate_coding_task)
    entry = cwt.registry.get_entry("delegate_coding_task")
    assert entry is not None
    route_decision = {
        "route": "ui_visual_specialist",
        "confidence": 0.91,
        "rationale": "visual implementation",
    }
    relevant_files = [{"path": "src/app.py", "note": "main implementation"}]
    scope_paths = ["src", "tests"]

    result = json.loads(
        entry.handler(
            {
                "task": "polish the AI budget dashboard",
                "route_decision": route_decision,
                "model_tier": "advanced",
                "reasoning_effort": "high",
                "relevant_files": relevant_files,
                "approach": "Patch the existing component.",
                "constraints": "Preserve the public props.",
                "verification": "Run the component tests.",
                "scope_paths": scope_paths,
            },
            parent_agent=_parent(tmp_path),
        )
    )

    assert result == {"success": True}
    assert captured["route_decision"] is route_decision
    assert captured["model_tier"] == "advanced"
    assert captured["reasoning_effort"] == "high"
    assert "worker_tier" not in captured
    assert captured["relevant_files"] is relevant_files
    assert captured["approach"] == "Patch the existing component."
    assert captured["constraints"] == "Preserve the public props."
    assert captured["verification"] == "Run the component tests."
    assert captured["scope_paths"] is scope_paths


def test_default_coding_worker_keeps_local_only_sanitized_codex_env(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example.invalid:8080")
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -F /custom/config")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/ssh-agent.sock")
    monkeypatch.setenv("GH_TOKEN", "gho_secret")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="fix without PR lifecycle",
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True
    kwargs = FakeSession.instances[0].kwargs
    assert kwargs["replace_env"] is False
    assert kwargs["env"]["HERMES_SESSION_KEY"] == "discord:123"
    assert kwargs["env"]["HTTPS_PROXY"] == "http://proxy.example.invalid:8080"
    assert kwargs["env"]["GIT_SSH_COMMAND"] == "ssh -F /custom/config"
    assert kwargs["env"]["SSH_AUTH_SOCK"] == "/tmp/ssh-agent.sock"
    assert "HERMES_CODEX_WORKER_NETWORK_ACCESS" not in kwargs["env"]
    assert "GH_TOKEN" not in kwargs["env"]
    assert "GITHUB_TOKEN" not in kwargs["env"]


def test_worker_env_fallback_does_not_leak_secrets(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("GH_CONFIG_DIR", "/home/droid/.config/gh")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/home/droid/.gitconfig")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/ssh-agent.sock")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    monkeypatch.setenv("GH_TOKEN", "gho_secret")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")

    env = cwt._coding_worker_fallback_env({"HERMES_SESSION_KEY": "discord:123"})

    assert env["PATH"] == "/usr/bin"
    assert env["GH_CONFIG_DIR"] == "/home/droid/.config/gh"
    assert env["GIT_CONFIG_GLOBAL"] == "/home/droid/.gitconfig"
    assert env["SSH_AUTH_SOCK"] == "/tmp/ssh-agent.sock"
    assert env["HERMES_SESSION_KEY"] == "discord:123"
    assert "OPENAI_API_KEY" not in env
    assert "GH_TOKEN" not in env
    assert "GITHUB_TOKEN" not in env


def test_ui_work_uses_normal_codex_model_tier(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["coding_worker"]["backend"] = "codex"
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="Review feedback: implement the frontend dashboard layout polish",
            context="Keep the Command Center responsive.",
            route_decision={
                "route": "ui_visual_specialist",
                "confidence": 0.86,
                "rationale": "review feedback requires visual implementation",
            },
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True
    route = result["ui_work_route"]
    assert route["matched"] is True
    assert route["enabled"] is True
    assert route["reason"] == "orchestrator route selected ui visual specialist"
    assert route["provider"] == ""
    assert route["model"] == ""
    assert route["backend"] == "codex"
    assert route["fallback_allowed"] is False
    assert route["error"] == ""
    assert route["route_decision"] == "ui_visual_specialist"
    assert route["route_decision_source"] == "orchestrator"
    assert route["route_decision_confidence"] == 0.86
    assert route["route_decision_rationale"] == "review feedback requires visual implementation"
    assert route["selected_route"] == "ui_visual_specialist"
    assert route["selected_provider"] == ""
    assert route["selected_model"] == ""
    assert route["fallback_used"] is False
    assert route["fallback_reason"] == ""
    assert route["advisory_matched"] is True
    assert "visual ui work" in route["advisory_reason"]
    assert route["recommended_skills"] == [
        "taste-skill",
        "claude-design",
        "popular-web-designs",
    ]
    assert route["actual_backend"] == "codex"
    assert route["actual_model"] == "gpt-5.6-sol"
    assert route["actual_reasoning_effort"] == "low"
    assert result["backend"] == "codex"
    assert len(FakeSession.instances) == 1
    assert FakeSession.instances[0].kwargs["extra_args"] == [
        "-c",
        'model="gpt-5.6-sol"',
        "-c",
        'model_reasoning_effort="low"',
        "-c",
        'service_tier="normal"',
    ]
    assert "UI specialist skill loading" in FakeSession.instances[0].run_calls[0]["user_input"]


def test_ui_work_smoke_title_uses_normal_codex_backend(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["coding_worker"]["backend"] = "codex"
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="Smoke-test UI specialist route on Command Center polish.",
            context="This is Command Center visual polish work; verify the UI specialist route.",
            route_decision={"route": "ui_visual_specialist"},
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True
    route = result["ui_work_route"]
    assert route["matched"] is True
    assert route["selected_route"] == "ui_visual_specialist"
    assert route["selected_provider"] == ""
    assert route["selected_model"] == ""
    assert route["advisory_matched"] is True
    assert "visual ui work" in route["advisory_reason"]
    assert len(FakeSession.instances) == 1


def test_explicit_default_route_keeps_default_codex_despite_visual_keywords(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["coding_worker"]["backend"] = "codex"
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="Implement the frontend dashboard layout polish",
            context="Keep the Command Center responsive.",
            route_decision={
                "route": "default_coding_worker",
                "confidence": 0.74,
                "rationale": "mostly data plumbing despite visual context",
            },
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True
    route = result["ui_work_route"]
    assert route["matched"] is False
    assert route["selected_route"] == "default_coding_worker"
    assert route["selected_provider"] == ""
    assert route["selected_model"] == ""
    assert route["route_decision_source"] == "orchestrator"
    assert route["route_decision_confidence"] == 0.74
    assert route["route_decision_rationale"] == "mostly data plumbing despite visual context"
    assert route["advisory_matched"] is True
    assert "visual ui work" in route["advisory_reason"]
    assert FakeSession.instances[0].kwargs["extra_args"] == [
        "-c",
        'model="gpt-5.6-sol"',
        "-c",
        'model_reasoning_effort="low"',
        "-c",
        'service_tier="normal"',
    ]


def test_unknown_route_decision_errors_before_worker_launch(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="Implement the frontend dashboard layout polish",
            route_decision={
                "route": "glm_visual",
                "confidence": 0.7,
                "rationale": "bad route",
            },
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is False
    assert result["status"] == "error"
    assert "unknown route_decision route" in result["error"]
    route = result["ui_work_route"]
    assert route["route_decision"] == "glm_visual"
    assert route["route_decision_source"] == "orchestrator"
    assert route["route_decision_confidence"] == 0.7
    assert route["route_decision_rationale"] == "bad route"
    assert route["selected_route"] == "default_coding_worker"
    assert FakeSession.instances == []


def test_legacy_ui_runtime_settings_do_not_change_codex_execution(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["coding_worker"]["backend"] = "codex"
    cfg["ui_work"].update(
        {
            "specialist_backend": "claude_code",
            "provider": "anthropic",
            "model": "claude-fable-5",
            "route": "anthropic_oauth",
            "reasoning_effort": "medium",
        }
    )
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="Implement frontend dashboard polish",
            route_decision={"route": "ui_visual_specialist"},
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True
    assert result["backend"] == "codex"
    assert result["ui_work_route"]["selected_route"] == "ui_visual_specialist"
    assert result["ui_work_route"]["selected_model"] == ""
    assert len(FakeSession.instances) == 1
    assert FakeSession.instances[0].kwargs["extra_args"] == [
        "-c",
        'model="gpt-5.6-sol"',
        "-c",
        'model_reasoning_effort="low"',
        "-c",
        'service_tier="normal"',
    ]


def test_tui_terminal_work_does_not_use_ui_model_overlay(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["coding_worker"]["backend"] = "codex"
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="Fix Hermes TUI terminal rendering layout in the session transcript",
            context="This is command-line TUI repaint work, not web UI development.",
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True
    assert result["ui_work_route"]["matched"] is False
    assert FakeSession.instances[0].kwargs["extra_args"] == [
        "-c",
        'model="gpt-5.6-sol"',
        "-c",
        'model_reasoning_effort="low"',
        "-c",
        'service_tier="normal"',
    ]


def test_ui_work_missing_legacy_model_still_launches_worker(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["ui_work"]["model"] = ""
    cfg["ui_work"]["fallback"] = {"allow_default_worker": False}
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="Polish frontend chart labels",
            route_decision={"route": "ui_visual_specialist"},
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True
    assert result["backend"] == "codex"
    assert len(FakeSession.instances) == 1


def test_delegate_prefers_hermes_md_context_over_agents(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    (tmp_path / "AGENTS.md").write_text("Agents rules should not be loaded.")
    (tmp_path / ".hermes.md").write_text("Hermes rules win.")
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="fix the parser",
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True
    prompt = FakeSession.instances[0].run_calls[0]["user_input"]
    assert "Repository context loaded by Hermes" in prompt
    assert "Hermes rules win." in prompt
    assert "Agents rules should not be loaded." not in prompt


def test_delegate_inherits_parent_preloaded_skill_context(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )
    parent = _parent(tmp_path)
    parent.ephemeral_system_prompt = "\n\n".join(
        [
            "base prompt",
            '[IMPORTANT: The user launched this CLI session with the "hermes-agent-dev" skill preloaded. Treat its instructions as active guidance for the duration of this session unless the user overrides them.]',
            "Use scripts/run_tests.sh for verification. Do not commit without permission.",
            "[Skill directory: /tmp/hermes-agent-dev]",
        ]
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="fix the parser",
            parent_agent=parent,
        )
    )

    assert result["success"] is True
    prompt = FakeSession.instances[0].run_calls[0]["user_input"]
    assert "Active skill instructions inherited from the parent Hermes session" in prompt
    assert "Omitted active parent skills passed as compact references" in prompt
    assert "hermes-agent-dev" in prompt
    assert "Use scripts/run_tests.sh for verification" in prompt
    assert "base prompt" not in prompt
    assert "skill instructions do not override this worker brief's ban" in prompt


def test_delegate_does_not_treat_post_skill_context_as_skill_context(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )
    parent = _parent(tmp_path)
    parent.ephemeral_system_prompt = "\n\n".join(
        [
            '[IMPORTANT: The user launched this CLI session with the "github-pr-workflow" skill preloaded. Treat its instructions as active guidance for the duration of this session unless the user overrides them.]',
            "Use gh pr checks before merge.",
            "[Skill directory: /tmp/github-pr-workflow]",
            "[System note: You are working in an isolated git worktree. Remember to commit and push your changes.]",
        ]
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="fix the parser",
            parent_agent=parent,
        )
    )

    assert result["success"] is True
    prompt = FakeSession.instances[0].run_calls[0]["user_input"]
    assert "github-pr-workflow" in prompt
    assert "Use gh pr checks before merge." in prompt
    assert "Remember to commit and push your changes" not in prompt


def test_delegate_inherits_runtime_skill_invocation_from_parent_messages(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )
    parent_messages = [
        {
            "role": "user",
            "content": "\n".join(
                [
                    '[IMPORTANT: The user has invoked the "autoreview" skill, indicating they want you to follow its instructions. The full skill content is loaded below.]',
                    "Run the autoreview helper after focused checks.",
                    "[Skill directory: /tmp/autoreview]",
                ]
            ),
        }
    ]

    result = json.loads(
        cwt.delegate_coding_task(
            task="fix the parser",
            parent_agent=_parent(tmp_path),
            parent_messages=parent_messages,
        )
    )

    assert result["success"] is True
    prompt = FakeSession.instances[0].run_calls[0]["user_input"]
    assert "Active skill instructions inherited from the parent Hermes session" in prompt
    assert "autoreview" in prompt
    assert "Run the autoreview helper after focused checks." in prompt


def test_delegate_always_includes_general_coding_full_body(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    _stub_general_coding(monkeypatch, content="General coding full body. Run focused checks.")
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="fix the parser",
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True
    prompt = FakeSession.instances[0].run_calls[0]["user_input"]
    assert "Full worker skill instructions" in prompt
    assert "General coding full body. Run focused checks." in prompt


def test_delegate_summarizes_inherited_hermes_and_pr_skills_by_default(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    monkeypatch.setattr(cwt, "_load_general_coding_skill", lambda: None)
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )
    parent = _parent(tmp_path)
    parent.ephemeral_system_prompt = "\n\n".join(
        [
            _skill_block(
                "hermes-agent",
                "Hermes summary line.\nFull Hermes body must stay omitted.",
                directory="/tmp/hermes-agent",
            ),
            _skill_block(
                "github-pr-workflow",
                "PR summary line.\nFull PR body must stay omitted.",
                directory="/tmp/github-pr-workflow",
            ),
        ]
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="fix the parser",
            parent_agent=parent,
        )
    )

    assert result["success"] is True
    prompt = FakeSession.instances[0].run_calls[0]["user_input"]
    assert "Omitted active parent skills passed as compact references" in prompt
    assert "hermes-agent: Hermes summary line" in prompt
    assert "Skill directory: /tmp/hermes-agent" in prompt
    assert "github-pr-workflow: PR summary line" in prompt
    assert "Skill directory: /tmp/github-pr-workflow" in prompt
    assert "Full Hermes body must stay omitted" not in prompt
    assert "Full PR body must stay omitted" not in prompt


def test_delegate_passes_full_body_for_explicit_worker_relevant_skill(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    monkeypatch.setattr(cwt, "_load_general_coding_skill", lambda: None)
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )
    parent = _parent(tmp_path)
    parent.ephemeral_system_prompt = _skill_block(
        "autoreview",
        "Autoreview summary line. Full autoreview worker instructions.",
        directory="/tmp/autoreview",
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="fix the parser\nworker-relevant skill: autoreview",
            parent_agent=parent,
        )
    )

    assert result["success"] is True
    prompt = FakeSession.instances[0].run_calls[0]["user_input"]
    assert "Full explicitly worker-relevant inherited skill instructions" in prompt
    assert "Full autoreview worker instructions." in prompt
    assert "Omitted active parent skills" not in prompt


def test_general_coding_does_not_consume_inherited_skill_budget(monkeypatch, tmp_path):
    monkeypatch.setattr(cwt, "_INHERITED_SKILL_CONTEXT_BUDGET_CHARS", 500)
    _stub_general_coding(monkeypatch, content="General coding full body. " + "g" * 2000)
    parent = _parent(tmp_path)
    parent.ephemeral_system_prompt = _skill_block(
        "hermes-agent",
        "Hermes compact summary.\nFull Hermes body must stay omitted.",
        directory="/tmp/hermes-agent",
    )

    context = cwt._parent_skill_context(parent)

    assert "General coding full body" in context
    assert "hermes-agent: Hermes compact summary" in context
    assert "Skill directory: /tmp/hermes-agent" in context
    assert "Full Hermes body must stay omitted" not in context


def test_parent_skill_context_budget_omits_oversized_relevant_skill(monkeypatch, tmp_path):
    monkeypatch.setattr(cwt, "_load_general_coding_skill", lambda: None)
    monkeypatch.setattr(cwt, "_INHERITED_SKILL_CONTEXT_BUDGET_CHARS", 250)
    parent = _parent(tmp_path)
    parent.ephemeral_system_prompt = _skill_block(
        "big-skill",
        "x" * 500,
        directory="/tmp/big-skill",
    )

    context = cwt._parent_skill_context(
        parent,
        task="fix bug\npass full skill: big-skill",
    )

    assert "Inherited skill context budget note" in context
    assert "250-character budget" in context
    assert "big-skill" not in context.split("Inherited skill context budget note", 1)[0]


def test_registry_parent_messages_dispatch_keeps_relevant_skill_full(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    monkeypatch.setattr(cwt, "_load_general_coding_skill", lambda: None)
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )
    parent_messages = [
        {
            "role": "user",
            "content": _skill_block(
                "autoreview",
                "Autoreview summary line. Registry dispatch full body.",
                directory="/tmp/autoreview",
            ),
        }
    ]

    result = json.loads(
        cwt.registry.dispatch(
            "delegate_coding_task",
            {
                "task": "fix the parser\nworker skill: autoreview",
                "_parent_messages": parent_messages,
            },
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True
    prompt = FakeSession.instances[0].run_calls[0]["user_input"]
    assert "Registry dispatch full body." in prompt


def test_delegate_does_not_add_skill_context_when_parent_has_no_loaded_skills(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )
    monkeypatch.setattr(cwt, "_load_general_coding_skill", lambda: None)
    parent = _parent(tmp_path)
    parent.ephemeral_system_prompt = "General non-skill instruction."

    result = json.loads(
        cwt.delegate_coding_task(
            task="fix the parser",
            parent_agent=parent,
        )
    )

    assert result["success"] is True
    prompt = FakeSession.instances[0].run_calls[0]["user_input"]
    assert "Active skill instructions inherited" not in prompt
    assert "General non-skill instruction" not in prompt


def test_delegate_reports_autoreview_materialization_failure(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )
    monkeypatch.setattr(
        "hermes_cli.worker_autoreview.materialize_autoreview_helper",
        lambda _workdir: (_ for _ in ()).throw(RuntimeError("readonly workspace")),
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="fix the parser",
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True
    prompt = FakeSession.instances[0].run_calls[0]["user_input"]
    assert "Autoreview helper materialization failed before worker start: readonly workspace" in prompt


def test_codex_backend_runs_plan_then_build_for_complex_task(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = [
        TurnResult(
            final_text="Plan: inspect auth boundary, patch parser, run tests.",
            thread_id="thread-plan",
            turn_id="turn-plan",
            tool_iterations=1,
        ),
        TurnResult(
            final_text="Implemented the auth fix and ran pytest.",
            thread_id="thread-build",
            turn_id="turn-build",
            tool_iterations=3,
        ),
    ]
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="fix production auth race",
            context="focus on src/auth.py",
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True
    assert result["backend"] == "codex"
    assert result["agents"] == ["plan", "build"]
    assert result["plan_used"] is True
    assert result["summary"] == "Implemented the auth fix and ran pytest."
    assert result["thread_id"] == "thread-build"
    assert result["tool_iterations"] == 4
    assert FakeSession.instances[0].kwargs["extra_args"] == [
        "-c",
        'model="gpt-5.6-sol"',
        "-c",
        'model_reasoning_effort="high"',
        "-c",
        'service_tier="normal"',
    ]
    assert FakeSession.instances[1].kwargs["extra_args"] == [
        "-c",
        'model="gpt-5.6-sol"',
        "-c",
        'model_reasoning_effort="low"',
        "-c",
        'service_tier="normal"',
    ]
    assert "Do not edit repository files" in FakeSession.instances[0].run_calls[0]["user_input"]
    assert "Codex plan to follow:" in FakeSession.instances[1].run_calls[0]["user_input"]


def test_codex_backend_uses_configured_reasoning_levels(monkeypatch, tmp_path):
    from agent import opencode_worker as ow

    FakeSession.instances = []
    FakeSession.results = [
        TurnResult(final_text="Plan", thread_id="thread-plan", turn_id="turn-plan"),
        TurnResult(final_text="Built", thread_id="thread-build", turn_id="turn-build"),
    ]
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )
    monkeypatch.setattr(
        ow,
        "load_coding_worker_pass_config",
        lambda config=None, worker_config=None: {
            "simple_build_reasoning_level": "low",
            "complex_plan_reasoning_level": "high",
            "complex_build_reasoning_level": "high",
        },
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="fix production auth race",
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True
    assert FakeSession.instances[0].kwargs["extra_args"] == [
        "-c",
        'model="gpt-5.6-sol"',
        "-c",
        'model_reasoning_effort="high"',
        "-c",
        'service_tier="normal"',
    ]
    assert FakeSession.instances[1].kwargs["extra_args"] == [
        "-c",
        'model="gpt-5.6-sol"',
        "-c",
        'model_reasoning_effort="high"',
        "-c",
        'service_tier="normal"',
    ]


def test_delegate_uses_opencode_backend_when_configured(monkeypatch, tmp_path):
    from agent import opencode_worker as ow

    (tmp_path / "AGENTS.md").write_text("OpenCode should see repo rules.")
    monkeypatch.setattr(ow, "load_coding_worker_backend", lambda: ow.BACKEND_OPENCODE)
    activity_messages = []
    parent = SimpleNamespace(
        api_mode="chat_completions",
        session_cwd=str(tmp_path),
        _touch_activity=activity_messages.append,
    )

    def fake_run(prompt, workspace, **kwargs):
        assert "fix the parser" in prompt
        assert "OpenCode should see repo rules." in prompt
        assert "workspace-local autoreview helper" in prompt
        assert workspace == str(tmp_path)
        assert kwargs["context_for_classification"]
        assert callable(kwargs["on_event"])
        kwargs["on_event"]({"type": "message", "agent": "build"})
        kwargs["on_event"](["unexpected event shape"])
        return SimpleNamespace(
            final_text="Changed src/parser.py and ran pytest.",
            error=None,
            interrupted=False,
            agents=["build"],
            plan_text="",
            thread_id="ses-build",
            turn_id="ses-build",
            tool_iterations=1,
        )

    monkeypatch.setattr(ow, "run_opencode_task", fake_run)

    result = json.loads(
        cwt.delegate_coding_task(
            task="fix the parser",
            context="focus on src/parser.py",
            parent_agent=parent,
        )
    )

    assert result["success"] is True
    assert result["backend"] == "opencode"
    assert result["agents"] == ["build"]
    assert result["summary"] == "Changed src/parser.py and ran pytest."
    assert activity_messages == ["OpenCode coding worker event: message: build"]
    assert parent.turn_worker_runs == [
        {
            "backend": "opencode",
            "model": "hermes-codex/gpt-5.6-sol",
            "reasoning": "low",
            "model_tier": None,
        },
    ]


def test_opus_turn_forces_codex_backend_over_configured_opencode(monkeypatch, tmp_path):
    from agent import opencode_worker as ow

    FakeSession.instances = []
    FakeSession.results = []
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["coding_worker"]["backend"] = "opencode"
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    monkeypatch.setattr(
        ow,
        "load_coding_worker_backend",
        lambda config=None, worker_config=None: ow.BACKEND_OPENCODE,
    )
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )
    parent = _parent(tmp_path)
    parent._opus_implementation_turn = True
    parent._coding_worker_backend_override = "codex"

    result = json.loads(
        cwt.delegate_coding_task(
            task="fix the parser",
            context="focus on src/parser.py",
            parent_agent=parent,
        )
    )

    assert result["success"] is True
    assert result["backend"] == "codex"
    assert len(FakeSession.instances) == 1


def test_opencode_exception_emits_failed_observer_closeout(monkeypatch, tmp_path):
    from agent import opencode_worker as ow

    manager = plugins.PluginManager()
    monkeypatch.setattr(plugins, "_plugin_manager", manager)
    starts = []
    stops = []
    manager._hooks["coding_worker_start"] = [lambda **kwargs: starts.append(kwargs)]
    manager._hooks["coding_worker_stop"] = [lambda **kwargs: stops.append(kwargs)]
    monkeypatch.setattr(ow, "load_coding_worker_backend", lambda: ow.BACKEND_OPENCODE)
    secret = "sk-secretsecretsecret"

    def fail_run(*_args, **_kwargs):
        raise RuntimeError(f"Authorization: Bearer {secret}")

    monkeypatch.setattr(ow, "run_opencode_task", fail_run)
    parent = _parent(tmp_path)
    parent.session_id = "parent-session"
    parent._current_turn_id = "parent-turn"

    with pytest.raises(RuntimeError, match="Authorization"):
        cwt.delegate_coding_task(task="fix parser", parent_agent=parent)

    assert len(starts) == len(stops) == 1
    assert stops[0]["worker_session_id"] == starts[0]["worker_session_id"]
    assert stops[0]["status"] == "failed"
    assert stops[0]["failed"] is True
    assert secret not in stops[0]["error"]
    assert id(parent.turn_worker_runs[0]) not in cwt._WORKER_OBSERVER_CONTEXTS


def test_observer_tool_result_inherits_preceding_call_name():
    messages = cwt._observer_safe_worker_messages(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "terminal",
                            "arguments": '{"cmd":"pytest"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "content": "passed",
            },
        ]
    )

    assert messages[1]["tool_name"] == "terminal"


def test_codex_exception_emits_failed_observer_closeout(monkeypatch, tmp_path):
    manager = plugins.PluginManager()
    monkeypatch.setattr(plugins, "_plugin_manager", manager)
    starts = []
    stops = []
    manager._hooks["coding_worker_start"] = [lambda **kwargs: starts.append(kwargs)]
    manager._hooks["coding_worker_stop"] = [lambda **kwargs: stops.append(kwargs)]
    secret = "sk-secretsecretsecret"

    class FailingSession(FakeSession):
        def run_turn(self, **kwargs):
            self.run_calls.append(kwargs)
            raise RuntimeError(f"Authorization: Bearer {secret}")

    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FailingSession,
    )
    parent = _parent(tmp_path)
    parent.session_id = "parent-session"
    parent._current_turn_id = "parent-turn"

    with pytest.raises(RuntimeError, match="Authorization"):
        cwt.delegate_coding_task(task="fix parser", parent_agent=parent)

    assert len(starts) == len(stops) == 1
    assert stops[0]["worker_session_id"] == starts[0]["worker_session_id"]
    assert stops[0]["status"] == "failed"
    assert stops[0]["failed"] is True
    assert secret not in stops[0]["error"]
    assert id(parent.turn_worker_runs[0]) not in cwt._WORKER_OBSERVER_CONTEXTS


def test_model_tier_overrides_opencode_model_and_reasoning(monkeypatch, tmp_path):
    from agent import opencode_worker as ow

    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["coding_worker"]["backend"] = "opencode"
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    monkeypatch.setattr(
        ow,
        "load_coding_worker_backend",
        lambda config=None, worker_config=None: ow.BACKEND_OPENCODE,
    )
    seen = {}

    def fake_run(prompt, workspace, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(
            final_text="done",
            error=None,
            interrupted=False,
            agents=["build"],
            plan_text="",
            thread_id="ses-build",
            turn_id="ses-build",
            tool_iterations=1,
        )

    monkeypatch.setattr(ow, "run_opencode_task", fake_run)

    result = json.loads(
        cwt.delegate_coding_task(
            task="fix the parser",
            model_tier="advanced",
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True
    assert seen["worker_config"] == {"model_tier": "advanced"}
    resolved = ow.load_opencode_config(cfg, worker_config=seen["worker_config"])
    assert resolved["simple_build_model"] == "hermes-codex/gpt-5.6-sol"
    assert resolved["complex_plan_model"] == "hermes-codex/gpt-5.6-sol"
    assert resolved["complex_build_model"] == "hermes-codex/gpt-5.6-sol"
    assert resolved["simple_build_reasoning_level"] == "high"
    assert resolved["complex_plan_reasoning_level"] == "high"
    assert resolved["complex_build_reasoning_level"] == "high"


def test_explicit_coding_worker_tier_is_not_keyword_rewritten(monkeypatch, tmp_path):
    from agent import opencode_worker as ow

    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["coding_worker"]["backend"] = "opencode"
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    monkeypatch.setattr(
        ow,
        "load_coding_worker_backend",
        lambda config=None, worker_config=None: ow.BACKEND_OPENCODE,
    )
    seen = {}

    def fake_run(prompt, workspace, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(
            final_text="No findings.",
            error=None,
            interrupted=False,
            agents=["build"],
            plan_text="",
            thread_id="ses-review",
            turn_id="ses-review",
            tool_iterations=1,
        )

    monkeypatch.setattr(ow, "run_opencode_task", fake_run)

    result = json.loads(
        cwt.delegate_coding_task(
            task="Audit the parser and report findings without changes",
            model_tier="advanced",
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True
    assert seen["worker_config"] == {"model_tier": "advanced"}
    resolved = ow.load_opencode_config(
        cfg,
        worker_config=seen["worker_config"],
        task="Audit the parser and report findings without changes",
    )
    assert resolved["simple_build_reasoning_level"] == "high"
    assert resolved["complex_plan_reasoning_level"] == "high"
    assert resolved["complex_build_reasoning_level"] == "high"


def test_exhausted_parent_deadline_blocks_coding_worker_launch(tmp_path):
    parent = _parent(tmp_path)
    parent._nested_worker_deadline_monotonic = 0.0

    result = json.loads(
        cwt.delegate_coding_task(task="fix the parser", parent_agent=parent)
    )

    assert "nested-worker deadline was exhausted" in result["error"]


def test_delegate_opencode_preserves_parent_scope_when_backend_supports_it(monkeypatch, tmp_path):
    from agent import opencode_worker as ow

    monkeypatch.setattr(ow, "load_coding_worker_backend", lambda: ow.BACKEND_OPENCODE)
    parent = _parent(tmp_path)
    seen = {}

    def fake_run(prompt, workspace, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(
            final_text="done",
            error=None,
            interrupted=False,
            agents=["build"],
            plan_text="",
            thread_id="ses-build",
            turn_id="ses-build",
            tool_iterations=1,
        )

    monkeypatch.setattr(ow, "run_opencode_task", fake_run)

    result = json.loads(cwt.delegate_coding_task(task="fix parser", parent_agent=parent))

    assert result["success"] is True
    assert seen["scope_session_key"] == "discord:123"


def test_preflight_repairs_canonical_cwd_to_existing_worktree(monkeypatch, tmp_path):
    canonical = tmp_path / "canonical" / "hermes"
    workspace_root = tmp_path / "workspaces"
    worktree = workspace_root / "hermes"
    canonical.mkdir(parents=True)
    worktree.mkdir(parents=True)
    parent = _parent(canonical)
    parent._coding_worker_required_this_turn = True

    monkeypatch.setattr(cwt, "_workspaces_path", lambda: workspace_root)
    monkeypatch.setattr(
        "tools.canonical_repo_guard.canonical_main_worker_violation",
        lambda workdir: "BLOCKED: delegate_coding_task was pointed at a protected canonical checkout",
    )
    monkeypatch.setattr(
        cwt,
        "_mutable_worktree_for_canonical_cwd",
        lambda workdir: str(worktree),
    )

    preflight = cwt.preflight_delegate_coding_task(
        {"task": "fix startup", "cwd": str(canonical), "context": "details"},
        parent,
    )

    assert preflight.suppressed_result is None
    assert preflight.args["cwd"] == str(worktree)
    assert "protected canonical cwd" in preflight.args["context"]


def test_preflight_suppresses_missing_worktree_for_required_canonical_cwd(monkeypatch, tmp_path):
    canonical = tmp_path / "canonical" / "hermes"
    canonical.mkdir(parents=True)
    parent = _parent(canonical)
    parent._coding_worker_required_this_turn = True

    monkeypatch.setattr(
        "tools.canonical_repo_guard.canonical_main_worker_violation",
        lambda workdir: "BLOCKED: delegate_coding_task was pointed at a protected canonical checkout",
    )
    monkeypatch.setattr(cwt, "_mutable_worktree_for_canonical_cwd", lambda workdir: None)

    preflight = cwt.preflight_delegate_coding_task(
        {"task": "fix startup", "cwd": str(canonical)},
        parent,
    )

    result = json.loads(preflight.suppressed_result)
    assert "could not find a mutable" in result["error"]
    assert "BLOCKED:" not in result["error"]


def test_fable_marker_does_not_add_a_git_worktree_requirement(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )
    monkeypatch.setattr(
        "agent.transports.codex_app_server.check_codex_binary",
        lambda: (True, "codex ready"),
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="Implement the feature",
            parent_agent=_fable_parent(tmp_path),
        )
    )

    assert result["success"] is True
    assert result["cwd"] == str(tmp_path)
    assert "fable_git_result" not in result


def test_fable_marker_uses_configured_opencode_backend(monkeypatch, tmp_path):
    from agent import opencode_worker as ow

    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"coding_worker": {"backend": "opencode"}})
    monkeypatch.setattr(
        ow,
        "load_coding_worker_backend",
        lambda config=None, worker_config=None: ow.BACKEND_OPENCODE,
    )
    monkeypatch.setattr(
        ow,
        "run_opencode_task",
        lambda *args, **kwargs: SimpleNamespace(
            final_text="done",
            error=None,
            interrupted=False,
            agents=["build"],
            plan_text="",
            thread_id="ses-build",
            turn_id="ses-build",
            tool_iterations=1,
        ),
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="Implement the feature",
            parent_agent=_fable_parent(tmp_path),
        )
    )

    assert result["success"] is True
    assert result["backend"] == "opencode"


def test_fable_parallel_group_uses_opencode_and_reports_merge_evidence(
    monkeypatch,
    tmp_path,
):
    from agent import opencode_worker as ow

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_worktree(repo)
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["coding_worker"]["backend"] = "opencode"
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    monkeypatch.setattr(
        ow,
        "load_coding_worker_backend",
        lambda config=None, worker_config=None: ow.BACKEND_OPENCODE,
    )
    monkeypatch.setattr(
        ow,
        "run_opencode_task",
        lambda *args, **kwargs: SimpleNamespace(
            final_text="done",
            error=None,
            interrupted=False,
            agents=["build"],
            plan_text="",
            thread_id="ses-build",
            turn_id="ses-build",
            tool_iterations=1,
        ),
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="Implement the feature",
            parent_agent=_fable_parent(repo),
            _parallel_group={"group_id": "fable-opencode", "base_cwd": str(repo)},
        )
    )

    assert result["success"] is True
    assert result["backend"] == "opencode"
    worker_cwd = Path(result["parallel"]["worker_cwd"])
    assert result["parallel"] == {
        "group_id": "fable-opencode",
        "worker_cwd": str(worker_cwd),
        "merged": False,
        "merge_pending": True,
        "merge_conflicts": [],
        "worktree_kept": True,
    }
    assert worker_cwd.exists()

    merge_result = cwt.merge_parallel_worker_result(
        str(repo),
        str(worker_cwd),
        "fable-opencode",
    )
    assert merge_result["merged"] is True
    assert not worker_cwd.exists()


def test_fable_marker_parallel_worker_returns_pending_merge_evidence(monkeypatch, tmp_path):
    from agent import opencode_worker as ow

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_worktree(repo)
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    monkeypatch.setattr(
        ow,
        "load_coding_worker_backend",
        lambda config=None, worker_config=None: ow.BACKEND_CODEX,
    )
    monkeypatch.setattr(
        "agent.transports.codex_app_server.check_codex_binary",
        lambda: (True, "codex ready"),
    )
    seen = {}

    class EditingSession(FakeSession):
        def run_turn(self, **kwargs):
            worker_cwd = Path(self.kwargs["cwd"])
            seen["worker_cwd"] = worker_cwd
            (worker_cwd / "src" / "app.py").write_text("fable result\n", encoding="utf-8")
            return super().run_turn(**kwargs)

    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        EditingSession,
    )
    monkeypatch.setattr(
        "hermes_cli.worker_autoreview.materialize_autoreview_helper",
        lambda workdir: None,
    )
    parent = _fable_parent(repo)

    result = json.loads(
        cwt.delegate_coding_task(
            task="build the parallel implementation",
            parent_agent=parent,
            _parallel_group={"group_id": "fable-merge", "base_cwd": str(repo)},
        )
    )

    assert result["success"] is True
    assert result["parallel"] == {
        "group_id": "fable-merge",
        "worker_cwd": str(seen["worker_cwd"]),
        "merged": False,
        "merge_pending": True,
        "merge_conflicts": [],
        "worktree_kept": True,
    }
    assert seen["worker_cwd"].exists()
    assert (repo / "src" / "app.py").read_text(encoding="utf-8") == "value = 1\n"

    merge_result = cwt.merge_parallel_worker_result(
        str(repo),
        str(seen["worker_cwd"]),
        "fable-merge",
    )
    assert merge_result["merged"] is True
    assert not seen["worker_cwd"].exists()


def test_delegate_opencode_omits_parent_scope_for_legacy_backend(monkeypatch, tmp_path):
    from agent import opencode_worker as ow

    monkeypatch.setattr(ow, "load_coding_worker_backend", lambda: ow.BACKEND_OPENCODE)
    seen = {}

    def fake_legacy_run(prompt, workspace, *, timeout, context_for_classification, title, on_event):
        seen.update(
            timeout=timeout,
            context_for_classification=context_for_classification,
            title=title,
            on_event=on_event,
        )
        return SimpleNamespace(
            final_text="done",
            error=None,
            interrupted=False,
            agents=["build"],
            plan_text="",
            thread_id="ses-build",
            turn_id="ses-build",
            tool_iterations=1,
        )

    monkeypatch.setattr(ow, "run_opencode_task", fake_legacy_run)

    result = json.loads(
        cwt.delegate_coding_task(
            task="system-doctor delegated worker after compaction",
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True
    assert seen["title"] == "Hermes delegated coding task"


def test_delegate_opencode_retries_when_passthrough_signature_rejects_scope(monkeypatch, tmp_path):
    from agent import opencode_worker as ow

    monkeypatch.setattr(ow, "load_coding_worker_backend", lambda: ow.BACKEND_OPENCODE)
    calls = []

    def fake_wrapper(prompt, workspace, **kwargs):
        calls.append(dict(kwargs))
        if "scope_session_key" in kwargs:
            raise TypeError("run_opencode_task() got an unexpected keyword argument 'scope_session_key'")
        return SimpleNamespace(
            final_text="done",
            error=None,
            interrupted=False,
            agents=["build"],
            plan_text="",
            thread_id="ses-build",
            turn_id="ses-build",
            tool_iterations=1,
        )

    monkeypatch.setattr(ow, "run_opencode_task", fake_wrapper)

    result = json.loads(
        cwt.delegate_coding_task(
            task="system-doctor delegated worker after compaction",
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True
    assert len(calls) == 2
    assert calls[0]["scope_session_key"] == "discord:123"
    assert "scope_session_key" not in calls[1]


def test_delegate_opencode_no_final_metadata_is_additive_and_degraded(monkeypatch, tmp_path):
    from agent import opencode_worker as ow

    monkeypatch.setattr(ow, "load_coding_worker_backend", lambda: ow.BACKEND_OPENCODE)
    metadata = {
        "classification": "no_final_text",
        "evidence_status": "degraded",
        "failure_class": "no_final_text",
        "backend": "opencode",
        "thread_id": "ses-empty",
        "turn_id": "ses-empty",
        "cwd": str(tmp_path),
        "branch": "feature",
        "commit": "abc123",
        "export_status": {"status": "empty", "session_id": "ses-empty"},
        "stderr_snippet": "",
        "error_snippet": "OpenCode completed without producing final text.",
        "local_file_changes": False,
        "local_commit_detected": False,
        "clean_committed_branch": False,
    }

    def fake_run(prompt, workspace, **kwargs):
        return SimpleNamespace(
            final_text="",
            error="OpenCode completed without producing final text.",
            interrupted=False,
            agents=["build"],
            plan_text="",
            thread_id="ses-empty",
            turn_id="ses-empty",
            tool_iterations=1,
            no_final_metadata=metadata,
        )

    monkeypatch.setattr(ow, "run_opencode_task", fake_run)

    result = json.loads(cwt.delegate_coding_task(task="fix parser", parent_agent=_parent(tmp_path)))

    assert result["success"] is False
    assert result["status"] == "partial"
    assert result["backend"] == "opencode"
    assert result["summary"] == ""
    assert result["error"] == "OpenCode completed without producing final text."
    assert result["evidence_status"] == "degraded"
    assert result["failure_class"] == "no_final_text"
    assert result["no_final_metadata"] == metadata


def test_delegate_opencode_no_final_clean_commit_is_recoverable(monkeypatch, tmp_path):
    from agent import opencode_worker as ow

    monkeypatch.setattr(ow, "load_coding_worker_backend", lambda: ow.BACKEND_OPENCODE)
    metadata = {
        "classification": "no_final_text",
        "evidence_status": "recoverable_degraded",
        "failure_class": "no_final_text",
        "backend": "opencode",
        "thread_id": "ses-committed",
        "turn_id": "ses-committed",
        "cwd": str(tmp_path),
        "branch": "feature",
        "commit": "def456",
        "export_status": {"status": "empty", "session_id": "ses-committed"},
        "stderr_snippet": "",
        "error_snippet": "OpenCode completed without producing final text.",
        "local_file_changes": False,
        "local_commit_detected": True,
        "clean_committed_branch": True,
    }

    def fake_run(prompt, workspace, **kwargs):
        return SimpleNamespace(
            final_text="",
            error="OpenCode completed without producing final text.",
            interrupted=False,
            agents=["build"],
            plan_text="",
            thread_id="ses-committed",
            turn_id="ses-committed",
            tool_iterations=1,
            no_final_metadata=metadata,
        )

    monkeypatch.setattr(ow, "run_opencode_task", fake_run)

    result = json.loads(cwt.delegate_coding_task(task="fix parser", parent_agent=_parent(tmp_path)))

    assert result["success"] is False
    assert result["evidence_status"] == "recoverable_degraded"
    assert result["failure_class"] == "no_final_text"
    assert result["no_final_metadata"]["local_commit_detected"] is True
    assert result["no_final_metadata"]["clean_committed_branch"] is True


def test_delegate_includes_repo_state_preflight(monkeypatch, tmp_path):
    from agent import opencode_worker as ow

    monkeypatch.setattr(ow, "load_coding_worker_backend", lambda: ow.BACKEND_OPENCODE)
    monkeypatch.setattr(
        cwt,
        "_repo_state_guard_notes",
        lambda workdir: "Repository state preflight:\n- concerns: dirty worktree",
    )

    def fake_run(prompt, workspace, **kwargs):
        assert "Repository state preflight:" in prompt
        assert "dirty worktree" in prompt
        assert "fix the parser" in prompt
        return SimpleNamespace(
            final_text="done",
            error=None,
            interrupted=False,
            agents=["build"],
            plan_text="",
            thread_id="ses-build",
            turn_id="ses-build",
            tool_iterations=1,
        )

    monkeypatch.setattr(ow, "run_opencode_task", fake_run)

    result = json.loads(
        cwt.delegate_coding_task(
            task="fix the parser",
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True


def test_prepare_pnpm_dependency_links_can_be_disabled(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    package = repo / "dashboard"
    source_package = worktree / "dashboard"
    package.mkdir(parents=True)
    source_package.mkdir(parents=True)
    for root in (package, source_package):
        (root / "package.json").write_text('{"name":"dashboard"}')
        (root / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
    (source_package / "node_modules").mkdir()

    monkeypatch.setenv("HERMES_CODING_WORKER_PNPM_LINKS", "0")

    assert cwt._prepare_pnpm_dependency_links(str(repo)) == []
    assert not (package / "node_modules").exists()


def test_prepare_pnpm_dependency_links_reuses_matching_worktree_when_enabled(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    package = repo / "dashboard"
    source_package = worktree / "dashboard"
    package.mkdir(parents=True)
    source_package.mkdir(parents=True)
    for root in (package, source_package):
        (root / "package.json").write_text('{"name":"dashboard"}')
        (root / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
    (source_package / "node_modules").mkdir()

    monkeypatch.setenv("HERMES_CODING_WORKER_PNPM_LINKS", "1")
    monkeypatch.setattr(
        "hermes_cli.worktree_runtime.repo_root_for_path",
        lambda path: repo,
    )
    monkeypatch.setattr(
        "hermes_cli.worktree_runtime.git_worktree_records",
        lambda root: [
            WorktreeRecord(str(worktree)),
            WorktreeRecord(str(repo)),
        ],
    )

    notes = cwt._prepare_pnpm_dependency_links(str(repo))

    assert notes == [
        f"linked {package / 'node_modules'} -> {source_package / 'node_modules'} "
        "(exact lock; unlink before running an install or changing dependencies)"
    ]
    assert (package / "node_modules").is_symlink()
    assert (package / "node_modules").resolve() == (source_package / "node_modules").resolve()


def test_prepare_pnpm_dependency_links_requires_matching_lock(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    package = repo / "dashboard"
    source_package = worktree / "dashboard"
    package.mkdir(parents=True)
    source_package.mkdir(parents=True)
    (package / "package.json").write_text('{"name":"dashboard"}')
    (package / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
    (source_package / "package.json").write_text('{"name":"dashboard"}')
    (source_package / "pnpm-lock.yaml").write_text("lockfileVersion: '8.0'\n")
    (source_package / "node_modules").mkdir()

    monkeypatch.setenv("HERMES_CODING_WORKER_PNPM_LINKS", "1")
    monkeypatch.setattr(
        "hermes_cli.worktree_runtime.repo_root_for_path",
        lambda path: repo,
    )
    monkeypatch.setattr(
        "hermes_cli.worktree_runtime.git_worktree_records",
        lambda root: [
            WorktreeRecord(str(worktree)),
            WorktreeRecord(str(repo)),
        ],
    )

    assert cwt._prepare_pnpm_dependency_links(str(repo)) == []
    assert not (package / "node_modules").exists()


def test_runs_with_available_codex_pool_credential(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    hermes_home = tmp_path / "hermes"
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
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="fix the parser",
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True
    codex_home = FakeSession.instances[0].kwargs["codex_home"]
    assert codex_home
    payload = FakeSession.instances[0].auth_payload
    assert payload is not None
    assert payload["tokens"]["access_token"] == "access-2"
    assert payload["tokens"]["refresh_token"] == "refresh-2"
    assert not Path(codex_home).exists()


def test_codex_worker_home_prefers_parent_current_credential(tmp_path):
    from agent.codex_worker_auth import prepare_codex_worker_home

    current_entry = SimpleNamespace(
        id="cred-current",
        access_token="access-current",
        refresh_token="refresh-current",
        id_token="id-current",
        last_status=None,
    )
    pool = SimpleNamespace(
        current=lambda: current_entry,
        select=MagicMock(),
    )
    parent = SimpleNamespace(provider="openai-codex", _credential_pool=pool)

    credential_id = prepare_codex_worker_home(tmp_path / "codex-home", parent_agent=parent)

    payload = json.loads((tmp_path / "codex-home" / "auth.json").read_text())
    assert credential_id == "cred-current"
    assert payload["tokens"]["access_token"] == "access-current"
    assert payload["tokens"]["refresh_token"] == "refresh-current"
    pool.select.assert_not_called()
