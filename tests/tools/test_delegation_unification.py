from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent.chat_completion_helpers import _coding_worker_tools_for_api
from agent.tool_dispatch_helpers import _should_parallelize_tool_batch
from agent.tool_executor import (
    _coding_worker_mutation_block,
    _delegation_mutation_block,
)
from tools import coding_worker_tool as cwt
from tools import delegate_tool as dt


def _call(name: str, **arguments):
    return SimpleNamespace(
        function=SimpleNamespace(
            name=name,
            arguments=json.dumps(arguments),
        )
    )


def test_read_only_delegate_fails_closed_but_keeps_repo_inspection():
    agent = SimpleNamespace(_delegation_read_only=True)

    assert _delegation_mutation_block(agent, "read_file", {"path": "x"}) is None
    assert _delegation_mutation_block(
        agent, "terminal", {"command": "git status --short --branch | head"}
    ) is not None
    assert _delegation_mutation_block(
        agent, "terminal", {"command": "git add . && git commit -m x"}
    ) is not None
    for command in (
        "git diff --output=review.patch",
        "git show --output=review.patch HEAD",
        "tree -o tree.txt",
    ):
        assert _delegation_mutation_block(
            agent,
            "terminal",
            {"command": command},
        ) is not None
    assert _delegation_mutation_block(agent, "write_file", {"path": "x"}) is not None
    assert _delegation_mutation_block(agent, "mcp_unknown_mutator", {}) is not None
    assert _delegation_mutation_block(agent, "request_coding_task", {}) is not None


def test_broker_only_delegate_can_request_broker_but_not_mutate_directly():
    agent = SimpleNamespace(
        _delegation_read_only=False,
        _delegation_broker_only_mutation=True,
    )

    assert _delegation_mutation_block(agent, "request_coding_task", {}) is None
    assert _delegation_mutation_block(agent, "patch", {}) is not None
    assert _delegation_mutation_block(agent, "delegate_coding_task", {}) is not None


def test_safe_background_analysis_and_coding_can_schedule_together():
    calls = [
        _call("delegate_task", goal="inspect", read_only=True, background=True),
        _call(
            "delegate_coding_task",
            task="edit api",
            scope_paths=["src/api"],
            background=True,
        ),
    ]

    assert _should_parallelize_tool_batch(calls) is True


def test_mixed_delegation_requires_read_only_analysis_but_not_background_mode():
    mutating_analysis = [
        _call("delegate_task", goal="edit", read_only=False, background=True),
        _call("delegate_coding_task", task="edit", scope_paths=["src"], background=True),
    ]
    synchronous_analysis = [
        _call("delegate_task", goal="inspect", read_only=True, background=False),
        _call("delegate_coding_task", task="edit", scope_paths=["src"], background=True),
    ]

    assert _should_parallelize_tool_batch(mutating_analysis) is False
    assert _should_parallelize_tool_batch(synchronous_analysis) is True


def test_parallel_background_coding_subset_requires_non_overlapping_scopes():
    accepted = [
        _call("delegate_task", goal="inspect", read_only=True, background=True),
        _call("delegate_coding_task", task="a", scope_paths=["src/a"], background=True),
        _call("delegate_coding_task", task="b", scope_paths=["src/b"], background=True),
    ]
    rejected = [
        _call("delegate_task", goal="inspect", read_only=True, background=True),
        _call("delegate_coding_task", task="a", scope_paths=["src"], background=True),
        _call("delegate_coding_task", task="b", scope_paths=["src/b"], background=True),
    ]

    assert _should_parallelize_tool_batch(accepted) is True
    assert _should_parallelize_tool_batch(rejected) is False


def test_global_mutation_reservations_reject_overlap_and_allow_isolated_siblings(tmp_path):
    first, error = cwt._acquire_mutation_reservation(
        cwd=str(tmp_path), scope_paths=["src"], parallel_group=None
    )
    assert first and error is None
    second, error = cwt._acquire_mutation_reservation(
        cwd=str(tmp_path), scope_paths=["docs"], parallel_group=None
    )
    assert second is None and "capacity reached" in error.lower()
    cwt._release_mutation_reservation(first)

    group = {"group_id": "g1"}
    left, error = cwt._acquire_mutation_reservation(
        cwd=str(tmp_path), scope_paths=["src/a"], parallel_group=group
    )
    right, error2 = cwt._acquire_mutation_reservation(
        cwd=str(tmp_path), scope_paths=["src/b"], parallel_group=group
    )
    assert left and right and error is None and error2 is None
    cwt._release_mutation_reservation(left)
    cwt._release_mutation_reservation(right)


def test_foreground_parallel_reservation_survives_merge_back(monkeypatch, tmp_path):
    worker = tmp_path / "worker"
    worker.mkdir()
    group = {"group_id": "g-merge", "base_cwd": str(tmp_path)}
    monkeypatch.setattr(
        cwt,
        "_delegate_coding_task_dispatch",
        lambda **kwargs: json.dumps(
            {
                "success": True,
                "status": "completed",
                "parallel": {
                    "group_id": "g-merge",
                    "worker_cwd": str(worker),
                    "merged": False,
                    "merge_pending": True,
                    "merge_conflicts": [],
                    "worktree_kept": True,
                },
            }
        ),
    )

    def merge(base_cwd, worker_cwd, group_id):
        blocked, error = cwt._acquire_mutation_reservation(
            cwd=str(tmp_path),
            scope_paths=["src"],
            parallel_group=None,
        )
        assert blocked is None
        assert "capacity reached" in error.lower()
        return {"group_id": group_id, "worker_cwd": worker_cwd, "merged": True}

    monkeypatch.setattr(cwt, "_merge_parallel_worker_result_locked", merge)
    result = cwt.delegate_coding_task(
        task="edit",
        cwd=str(tmp_path),
        scope_paths=["src"],
        _parallel_group=group,
    )
    assert json.loads(result)["parallel"]["merge_pending"] is True
    assert cwt._MUTATION_RESERVATIONS

    merged = cwt.merge_parallel_worker_result(str(tmp_path), str(worker), "g-merge")
    assert merged["merged"] is True
    assert cwt._MUTATION_RESERVATIONS == {}
    assert cwt._PARALLEL_WORKER_RESERVATIONS == {}


def test_background_parallel_reservation_releases_after_result_cache(
    monkeypatch,
    tmp_path,
):
    worker = tmp_path / "worker"
    worker.mkdir()
    group = {"group_id": "g-background", "base_cwd": str(tmp_path)}
    reservation_id, error = cwt._acquire_mutation_reservation(
        cwd=str(tmp_path),
        scope_paths=["src"],
        parallel_group=group,
    )
    assert reservation_id and error is None
    raw = json.dumps(
        {
            "parallel": {
                "worker_cwd": str(worker),
                "merge_pending": True,
            }
        }
    )
    assert cwt._transfer_parallel_worker_reservation(raw, reservation_id) is True
    with cwt._BACKGROUND_PARALLEL_WORKERS_GUARD:
        cwt._BACKGROUND_PARALLEL_WORKERS.add(str(worker.resolve()))

    def merge(base_cwd, worker_cwd, group_id):
        blocked, blocked_error = cwt._acquire_mutation_reservation(
            cwd=str(tmp_path),
            scope_paths=["src"],
            parallel_group=None,
        )
        assert blocked is None
        assert "capacity reached" in blocked_error.lower()
        return {"group_id": group_id, "worker_cwd": worker_cwd, "merged": True}

    monkeypatch.setattr(cwt, "_merge_parallel_worker_result_locked", merge)
    startup = cwt._BackgroundCodingStartup(
        task="edit",
        context_pack={},
        parallel_group=group,
        worker_cwd=str(worker),
    )
    payload = {"success": True, "status": "completed"}

    cwt._complete_background_parallel_result(payload, startup)

    assert payload["parallel_merge"]["merged"] is True
    assert cwt._MUTATION_RESERVATIONS == {}
    assert cwt._PARALLEL_WORKER_RESERVATIONS == {}
    with cwt._BACKGROUND_PARALLEL_WORKERS_GUARD:
        assert str(worker.resolve()) in cwt._BACKGROUND_PARALLEL_RESULTS
        cwt._BACKGROUND_PARALLEL_RESULTS.pop(str(worker.resolve()), None)


def test_parallel_reservation_releases_when_merge_back_fails(monkeypatch, tmp_path):
    worker = tmp_path / "worker-failure"
    worker.mkdir()
    reservation_id, error = cwt._acquire_mutation_reservation(
        cwd=str(tmp_path),
        scope_paths=["src"],
        parallel_group={"group_id": "g-failure"},
    )
    assert reservation_id and error is None
    raw = json.dumps(
        {"parallel": {"worker_cwd": str(worker), "merge_pending": True}}
    )
    assert cwt._transfer_parallel_worker_reservation(raw, reservation_id) is True
    monkeypatch.setattr(
        cwt,
        "_merge_parallel_worker_result_locked",
        lambda *args: (_ for _ in ()).throw(RuntimeError("merge failed")),
    )

    try:
        cwt.merge_parallel_worker_result(
            str(tmp_path),
            str(worker),
            "g-failure",
        )
    except RuntimeError as exc:
        assert "merge failed" in str(exc)
    else:
        raise AssertionError("merge failure should propagate to its caller")

    assert cwt._MUTATION_RESERVATIONS == {}
    assert cwt._PARALLEL_WORKER_RESERVATIONS == {}


def test_nested_coding_grant_rejects_background_and_read_only(monkeypatch):
    parent = SimpleNamespace(_delegate_depth=0)
    monkeypatch.setattr(dt, "_get_nested_coding_enabled", lambda: True)

    background = json.loads(
        dt.delegate_task(
            goal="orchestrate",
            role="orchestrator",
            allow_nested_coding=True,
            background=True,
            parent_agent=parent,
        )
    )
    read_only = json.loads(
        dt.delegate_task(
            goal="orchestrate",
            role="orchestrator",
            allow_nested_coding=True,
            read_only=True,
            parent_agent=parent,
        )
    )

    assert "foreground-only" in background["error"]
    assert "Read-only" in read_only["error"]


def test_batch_nested_coding_grants_fail_atomically_before_child_construction(
    monkeypatch,
):
    parent = SimpleNamespace(_delegate_depth=0)
    built = []
    monkeypatch.setattr(dt, "_background_context_error", lambda _parent: "")
    monkeypatch.setattr(dt, "_get_nested_coding_enabled", lambda: True)
    monkeypatch.setattr(dt, "_get_orchestrator_enabled", lambda: True)
    monkeypatch.setattr(dt, "_get_max_spawn_depth", lambda: 2)
    monkeypatch.setattr(dt, "_build_child_agent", lambda **kwargs: built.append(kwargs))

    result = json.loads(
        dt.delegate_task(
            tasks=[
                {"goal": "ordinary analysis"},
                {
                    "goal": "invalid broker request",
                    "role": "leaf",
                    "allow_nested_coding": True,
                },
            ],
            parent_agent=parent,
        )
    )

    assert "requires role='orchestrator'" in result["error"]
    assert built == []


def test_explicit_nested_coding_grant_rejects_silent_leaf_degradation(monkeypatch):
    parent = SimpleNamespace(_delegate_depth=0)
    monkeypatch.setattr(dt, "_get_nested_coding_enabled", lambda: True)
    monkeypatch.setattr(dt, "_get_orchestrator_enabled", lambda: False)
    monkeypatch.setattr(dt, "_get_max_spawn_depth", lambda: 2)

    disabled = json.loads(
        dt.delegate_task(
            goal="orchestrate",
            role="orchestrator",
            allow_nested_coding=True,
            parent_agent=parent,
        )
    )

    monkeypatch.setattr(dt, "_get_orchestrator_enabled", lambda: True)
    monkeypatch.setattr(dt, "_get_max_spawn_depth", lambda: 1)
    shallow = json.loads(
        dt.delegate_task(
            goal="orchestrate",
            role="orchestrator",
            allow_nested_coding=True,
            parent_agent=parent,
        )
    )

    assert "orchestrator_enabled=true" in disabled["error"]
    assert "max_spawn_depth>=2" in shallow["error"]


def test_coding_required_api_narrowing_retains_analysis_delegation():
    tools = [
        {"function": {"name": "read_file"}},
        {"function": {"name": "delegate_task"}},
        {"function": {"name": "delegate_coding_task"}},
    ]
    agent = SimpleNamespace(
        _coding_worker_required_this_turn=True,
        _coding_worker_used_this_turn=False,
        api_mode="chat_completions",
    )

    narrowed = _coding_worker_tools_for_api(agent, tools)
    assert [item["function"]["name"] for item in narrowed] == [
        "delegate_task",
        "delegate_coding_task",
    ]


def test_fable_uses_normal_post_worker_mutation_guardrail():
    agent = SimpleNamespace(
        _fable_implementation_turn=True,
        _coding_worker_required_this_turn=True,
        _coding_worker_used_this_turn=True,
        api_mode="chat_completions",
    )

    assert _coding_worker_mutation_block(
        agent, "terminal", {"command": "git add ."}
    ) is None
    assert _coding_worker_mutation_block(agent, "delegate_task", {}) is None


def test_delegate_schema_preserves_compatibility_and_adds_new_gates():
    properties = dt.DELEGATE_TASK_SCHEMA["parameters"]["properties"]
    task_properties = properties["tasks"]["items"]["properties"]

    for name in ("toolsets", "acp_command", "acp_args"):
        assert name in properties
    for name in ("toolsets", "acp_command", "acp_args"):
        assert name in task_properties
    assert properties["background"]["default"] is False
    assert "read_only" in properties
    assert "allow_nested_coding" in properties


def test_structured_handoffs_are_deep_copied_and_reject_stale_identity(tmp_path):
    root = SimpleNamespace(
        session_key="agent:main:discord:thread:1:2",
        session_id="session-1",
        _current_task_id="turn-1",
        _origin_work_item_id="work-1",
        session_cwd=str(tmp_path),
    )
    handoff = {
        "handoff_id": "handoff_1",
        "created_at": time.time(),
        "read_only": False,
        "binding": dt._delegation_binding(root),
        "files_read": ["a.py"],
        "broker_results": [{"result": {"scope_check": {"clean": True}}}],
    }
    root._delegation_handoffs = {"handoff_1": handoff}
    child = SimpleNamespace(_delegate_root_agent=root)

    resolved, error = cwt._resolve_analysis_handoffs(child, ["handoff_1"])
    missing, missing_error = cwt._resolve_analysis_handoffs(child, ["fabricated"])

    assert error is None and resolved == [handoff]
    resolved[0]["broker_results"][0]["result"]["scope_check"]["clean"] = False
    assert root._delegation_handoffs["handoff_1"]["broker_results"][0]["result"][
        "scope_check"
    ]["clean"] is True
    assert missing == [] and "Unknown" in missing_error

    root._current_task_id = "turn-2"
    stale, stale_error = cwt._resolve_analysis_handoffs(child, ["handoff_1"])
    assert stale == []
    assert "Stale or mismatched" in stale_error
    assert "handoff_1" not in root._delegation_handoffs


def test_missing_handoff_registry_does_not_block_explicit_coding_task():
    parent = SimpleNamespace()

    resolved, error = cwt._resolve_analysis_handoffs(parent, ["handoff_lost"])

    assert resolved == []
    assert error is None


def test_recent_read_only_handoff_can_cross_completion_turn(tmp_path):
    root = SimpleNamespace(
        session_key="agent:main:discord:thread:1:2",
        session_id="session-1",
        _current_task_id="turn-dispatch",
        _origin_work_item_id="work-1",
        session_cwd=str(tmp_path),
    )
    handoff = {
        "handoff_id": "handoff_async",
        "created_at": time.time(),
        "read_only": True,
        "binding": dt._delegation_binding(root),
    }
    root._delegation_handoffs = {"handoff_async": handoff}
    root._current_task_id = "turn-completion"

    resolved, error = cwt._resolve_analysis_handoffs(
        SimpleNamespace(_delegate_root_agent=root),
        ["handoff_async"],
    )

    assert error is None
    assert resolved == [handoff]


def test_background_delegate_requires_read_only_and_rejects_capacity(monkeypatch):
    parent = MagicMock()
    parent._delegate_depth = 0
    parent._delegation_read_only = False
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent.gateway_session_key = "agent:main:cli:dm:local"
    parent.session_key = "agent:main:cli:dm:local"
    parent.session_id = "session-1"
    parent.session_cwd = "/tmp"
    child = MagicMock()
    child.tool_progress_callback = None
    monkeypatch.setattr(dt, "_background_context_error", lambda _parent: "")
    monkeypatch.setattr(
        dt,
        "_resolve_delegation_credentials",
        lambda *_args, **_kwargs: {
            "model": None,
            "provider": None,
            "base_url": None,
            "api_key": None,
            "api_mode": None,
            "command": None,
            "args": None,
        },
    )
    monkeypatch.setattr(dt, "_build_child_agent", lambda **_kwargs: child)
    monkeypatch.setattr(
        dt,
        "_run_single_child",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("rejected background work must not run synchronously")
        ),
    )

    unsafe = json.loads(
        dt.delegate_task(goal="edit", background=True, parent_agent=parent)
    )
    assert "requires read_only=true" in unsafe["error"]

    monkeypatch.setattr(
        "tools.async_delegation.dispatch_async_delegation_batch",
        lambda **_kwargs: {"status": "rejected", "error": "capacity reached"},
    )
    rejected = json.loads(
        dt.delegate_task(
            goal="inspect",
            read_only=True,
            background=True,
            parent_agent=parent,
        )
    )

    assert rejected["error"] == "capacity reached"
    child.close.assert_called_once()


def test_request_coding_task_forces_root_owned_context(monkeypatch, tmp_path):
    captured = {}
    root = SimpleNamespace(_brokered_coding_results={})
    child = SimpleNamespace(
        _delegate_role="orchestrator",
        _delegation_read_only=False,
        _subagent_id="sa-1",
        _delegation_broker_context={
            "enabled": True,
            "root_agent": root,
            "authorized_cwd": str(tmp_path),
            "gateway_session_key": "agent:main:discord:x",
            "session_id": "session-root",
            "origin_work_item_id": "work-1",
            "visual_qa_requirement": {"level": "surface", "target": "home"},
            "project_inspection_candidates": [
                {
                    "url": "http://localhost:3000",
                    "environment": "development",
                    "location": "local",
                }
            ],
        },
    )

    def fake_delegate(**kwargs):
        captured.update(kwargs)
        return json.dumps(
            {
                "success": True,
                "status": "completed",
                "cwd": kwargs["cwd"],
                "backend": "codex",
                "scope_check": {"ok": True},
            }
        )

    monkeypatch.setattr(cwt, "delegate_coding_task", fake_delegate)
    result = json.loads(
        dt.request_coding_task(
            task="implement",
            context="ctx",
            model_tier="trivial",
            reasoning_effort="high",
            relevant_files=[{"path": "a.py", "note": "entry"}],
            approach="small patch",
            constraints="keep api",
            verification="focused test",
            scope_paths=["src"],
            analysis_handoff_ids=["handoff_1"],
            route_decision={
                "route": "ui_visual_specialist",
                "visual_advisor_tier": "advanced",
            },
            parent_agent=child,
        )
    )

    assert captured["cwd"] == str(tmp_path)
    assert captured["background"] is False
    assert captured["allow_git_pr_lifecycle"] is False
    assert captured["trusted_allow_git_pr_lifecycle"] is False
    assert captured["parent_agent"] is root
    assert captured["model_tier"] == "trivial"
    assert captured["reasoning_effort"] == "high"
    assert captured["route_decision"] == {
        "route": "ui_visual_specialist",
        "visual_advisor_tier": "advanced",
    }
    assert "worker_tier" not in captured
    assert root._coding_worker_used_this_turn is True
    assert captured["visual_qa_requirement"]["level"] == "surface"
    assert captured["project_inspection_candidates"][0]["url"].endswith(":3000")
    assert result["broker"]["origin_work_item_id"] == "work-1"
    result_id = result["broker"]["result_id"]
    assert root._brokered_coding_results[result_id]["result"]["scope_check"] == {
        "ok": True
    }


def test_request_coding_task_serializes_same_root_workspace(monkeypatch, tmp_path):
    state = {"active": 0, "max_active": 0}
    forwarded = []
    gate = threading.Barrier(2)
    root = SimpleNamespace(_brokered_coding_results={})

    def child(name):
        return SimpleNamespace(
            _delegate_role="orchestrator",
            _delegation_read_only=False,
            _subagent_id=name,
            _delegation_broker_context={
                "enabled": True,
                "root_agent": root,
                "authorized_cwd": str(tmp_path),
            },
        )

    def fake_delegate(**kwargs):
        forwarded.append(kwargs)
        state["active"] += 1
        state["max_active"] = max(state["max_active"], state["active"])
        time.sleep(0.05)
        state["active"] -= 1
        return json.dumps({"success": True, "status": "completed"})

    monkeypatch.setattr(cwt, "delegate_coding_task", fake_delegate)

    def run(parent):
        gate.wait()
        dt.request_coding_task(
            task="x",
            context=None,
            model_tier=None,
            reasoning_effort=None,
            relevant_files=None,
            approach=None,
            constraints=None,
            verification=None,
            scope_paths=["src"],
            analysis_handoff_ids=None,
            route_decision=None,
            parent_agent=parent,
        )

    threads = [threading.Thread(target=run, args=(child(f"sa-{i}"),)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert state["max_active"] == 1
    assert len(forwarded) == 2
    assert all(call.get("model_tier") is None for call in forwarded)
    assert all(call.get("reasoning_effort") is None for call in forwarded)
    assert all("worker_tier" not in call for call in forwarded)


def test_background_delegate_falls_back_to_sync_without_delivery(monkeypatch):
    parent = MagicMock()
    parent._delegate_depth = 0
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    monkeypatch.setattr(dt, "_get_max_spawn_depth", lambda: 2)
    fake_child = MagicMock()
    fake_child._delegate_role = "leaf"
    fake_child._subagent_id = "sa-bg"
    monkeypatch.setattr(dt, "_background_context_error", lambda _parent: "no delivery")
    monkeypatch.setattr(
        dt,
        "_resolve_delegation_credentials",
        lambda *_args, **_kwargs: {
            "model": None,
            "provider": None,
            "base_url": None,
            "api_key": None,
            "api_mode": None,
            "command": None,
            "args": None,
        },
    )
    monkeypatch.setattr(dt, "_build_child_agent", lambda **_kwargs: fake_child)
    monkeypatch.setattr(
        dt,
        "_run_single_child",
        lambda *_args, **_kwargs: {
            "task_index": 0,
            "status": "completed",
            "summary": "sync fallback",
            "api_calls": 1,
            "duration_seconds": 0.1,
        },
    )

    result = json.loads(
        dt.delegate_task(goal="inspect", background=True, parent_agent=parent)
    )

    assert result["results"][0]["summary"] == "sync fallback"
    assert result.get("mode") != "background"


def test_detached_accounting_uses_dispatch_identity_and_safe_owner(monkeypatch):
    hooks = []
    memory = MagicMock()
    parent = SimpleNamespace(
        session_key="agent:main:discord:thread:1:2",
        session_id="session-1",
        session_cwd="/tmp/repo",
        _current_task_id="turn-dispatch",
        _memory_manager=memory,
        session_estimated_cost_usd=0.1,
        session_cost_source="none",
        session_cost_status="unknown",
    )
    context = dt._capture_detached_accounting_context(parent)
    child = SimpleNamespace(session_id="child-1")
    results = [
        {
            "task_index": 0,
            "status": "completed",
            "summary": "done",
            "duration_seconds": 0.25,
            "_child_role": "leaf",
            "_child_cost_usd": 0.3,
        }
    ]
    monkeypatch.setattr(
        "hermes_cli.plugins.invoke_hook",
        lambda name, **kwargs: hooks.append((name, kwargs)),
    )

    accounting = dt._finalize_detached_results(
        results,
        [(0, {"goal": "inspect"}, child)],
        context,
    )

    assert parent.session_estimated_cost_usd == 0.1
    memory.on_delegation.assert_not_called()
    assert hooks == []
    assert "_child_role" not in results[0]
    assert "_child_cost_usd" not in results[0]

    parent._current_task_id = "later-turn"
    assert dt.apply_detached_delegation_accounting(parent, accounting) is True
    assert parent.session_estimated_cost_usd == 0.4
    memory.on_delegation.assert_called_once_with(
        task="inspect",
        result="done",
        child_session_id="child-1",
    )
    assert hooks[0][0] == "subagent_stop"
    assert hooks[0][1]["parent_turn_id"] == "turn-dispatch"
    assert dt.apply_detached_delegation_accounting(parent, accounting) is False
    assert parent.session_estimated_cost_usd == 0.4


def test_read_only_policy_propagates_to_nested_delegate_calls(monkeypatch):
    parent = MagicMock()
    parent._delegate_depth = 1
    parent._delegation_read_only = True
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    monkeypatch.setattr(dt, "_get_max_spawn_depth", lambda: 2)
    captured = {}
    fake_child = MagicMock()
    fake_child._delegate_role = "leaf"
    monkeypatch.setattr(
        dt,
        "_resolve_delegation_credentials",
        lambda *_args, **_kwargs: {
            "model": None,
            "provider": None,
            "base_url": None,
            "api_key": None,
            "api_mode": None,
            "command": None,
            "args": None,
        },
    )

    def build(**kwargs):
        captured.update(kwargs)
        return fake_child

    monkeypatch.setattr(dt, "_build_child_agent", build)
    monkeypatch.setattr(
        dt,
        "_run_single_child",
        lambda *_args, **_kwargs: {
            "task_index": 0,
            "status": "completed",
            "summary": "ok",
            "api_calls": 1,
            "duration_seconds": 0.1,
        },
    )

    dt.delegate_task(goal="nested inspection", read_only=False, parent_agent=parent)

    assert captured["read_only"] is True


def test_coding_worker_registry_preserves_legacy_membership_and_atomic_subtraction():
    from tools.registry import registry

    assert registry.get_entry("delegate_coding_task").toolset == "delegation"
    assert registry.get_entry("request_coding_task").toolset == "delegated_coding_broker"

    import model_tools

    ordinary = model_tools.get_tool_definitions(
        enabled_toolsets=["hermes-cli"],
        disabled_toolsets=["coding_worker_raw", "delegated_coding_broker"],
        quiet_mode=True,
        skip_tool_search_assembly=True,
    )
    ordinary_names = {(item.get("function") or {}).get("name") for item in ordinary}
    assert "delegate_coding_task" not in ordinary_names
    assert "request_coding_task" not in ordinary_names

    brokered = model_tools.get_tool_definitions(
        enabled_toolsets=["hermes-cli", "delegated_coding_broker"],
        disabled_toolsets=["coding_worker_raw"],
        quiet_mode=True,
        skip_tool_search_assembly=True,
    )
    brokered_names = {(item.get("function") or {}).get("name") for item in brokered}
    assert "delegate_coding_task" not in brokered_names
    assert "request_coding_task" in brokered_names

    legacy_enabled = model_tools.get_tool_definitions(
        enabled_toolsets=["delegation"],
        quiet_mode=True,
        skip_tool_search_assembly=True,
    )
    legacy_enabled_names = {
        (item.get("function") or {}).get("name") for item in legacy_enabled
    }
    assert {"delegate_task", "delegate_coding_task"} <= legacy_enabled_names

    legacy_disabled = model_tools.get_tool_definitions(
        enabled_toolsets=["hermes-cli"],
        disabled_toolsets=["delegation"],
        quiet_mode=True,
        skip_tool_search_assembly=True,
    )
    legacy_disabled_names = {
        (item.get("function") or {}).get("name") for item in legacy_disabled
    }
    assert "delegate_task" not in legacy_disabled_names
    assert "delegate_coding_task" not in legacy_disabled_names


def test_worker_layer_has_no_parallel_fable_lifecycle_state_machine():
    assert not hasattr(cwt, "_parallel_fable_state")
    assert not hasattr(cwt, "complete_parallel_fable_lifecycle")
    assert not hasattr(cwt, "_persist_fable_closeout")
