from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace


def _parent(*, depth: int = 0) -> SimpleNamespace:
    return SimpleNamespace(_delegate_depth=depth)


def test_is_registered_in_the_core_toolset():
    import tools.canonical_checkout_sync_tool  # noqa: F401
    from toolsets import _HERMES_CORE_TOOLS
    from tools.registry import registry

    assert "sync_canonical_checkout" in _HERMES_CORE_TOOLS
    assert registry.get_entry("sync_canonical_checkout") is not None


def test_is_hidden_from_dispatcher_scoped_worker_schema(monkeypatch):
    import tools.canonical_checkout_sync_tool  # noqa: F401
    from toolsets import resolve_toolset
    from tools.registry import invalidate_check_fn_cache, registry

    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-123")
    invalidate_check_fn_cache()
    definitions = registry.get_definitions(set(resolve_toolset("hermes-cli")), quiet=True)
    names = {item["function"]["name"] for item in definitions if "function" in item}

    assert "sync_canonical_checkout" not in names


def test_registry_dispatch_without_parent_is_refused(tmp_path: Path):
    from tools.canonical_checkout_sync_tool import _registry_handler

    result = json.loads(
        _registry_handler(
            {
                "project_path": str(tmp_path),
                "branch": "main",
                "merge_commit": "a" * 40,
            }
        )
    )

    assert "trusted Hermes orchestrator dispatch" in result["error"]


def test_refuses_dispatcher_workers_and_delegated_agents(monkeypatch, tmp_path: Path):
    from tools.canonical_checkout_sync_tool import sync_canonical_checkout_tool

    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-123")
    worker_result = json.loads(
        sync_canonical_checkout_tool(
            project_path=str(tmp_path),
            branch="main",
            merge_commit="a" * 40,
            parent_agent=_parent(),
        )
    )
    assert "orchestrator-only" in worker_result["error"]

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    child_result = json.loads(
        sync_canonical_checkout_tool(
            project_path=str(tmp_path),
            branch="main",
            merge_commit="a" * 40,
            parent_agent=_parent(depth=1),
        )
    )
    assert "parent Hermes orchestrator" in child_result["error"]


def test_rejects_noncanonical_target_before_running_git(monkeypatch, tmp_path: Path):
    from tools import canonical_checkout_sync_tool as sync_tool

    monkeypatch.setattr(sync_tool, "_canonical_root", lambda *_args: (None, "not canonical"))
    result = json.loads(
        sync_tool.sync_canonical_checkout_tool(
            project_path=str(tmp_path),
            branch="main",
            merge_commit="a" * 40,
            parent_agent=_parent(),
        )
    )

    assert result["error"] == "not canonical"


def test_rejects_non_exact_merge_sha_lengths_before_root_resolution(monkeypatch, tmp_path: Path):
    from tools import canonical_checkout_sync_tool as sync_tool

    root_calls = []
    monkeypatch.setattr(sync_tool, "_canonical_root", lambda *_args: root_calls.append(_args))

    for invalid in ("a" * 7, "a" * 41, "a" * 63):
        result = json.loads(
            sync_tool.sync_canonical_checkout_tool(
                project_path=str(tmp_path),
                branch="main",
                merge_commit=invalid,
                parent_agent=_parent(),
            )
        )
        assert "exact 40- or 64-character" in result["error"]
    assert root_calls == []


def test_syncs_only_after_root_and_merge_sha_validation(monkeypatch, tmp_path: Path):
    from hermes_cli.canonical_checkout_sync import CanonicalCheckoutSyncResult
    from tools import canonical_checkout_sync_tool as sync_tool

    calls: list[tuple[Path, str, str]] = []
    monkeypatch.setattr(sync_tool, "_canonical_root", lambda *_args: (tmp_path, None))

    def fake_sync(path: Path, branch: str, merge_commit: str):
        calls.append((path, branch, merge_commit))
        return CanonicalCheckoutSyncResult(
            state="synced",
            error="",
            path=str(path),
            branch=branch,
            head="canonical-head",
            merge_commit=merge_commit,
            synced_at="2026-07-14T16:17:31Z",
        )

    monkeypatch.setattr("hermes_cli.canonical_checkout_sync.sync_canonical_checkout", fake_sync)
    result = json.loads(
        sync_tool.sync_canonical_checkout_tool(
            project_path=str(tmp_path),
            branch="main",
            merge_commit="a" * 40,
            parent_agent=_parent(),
        )
    )

    assert calls == [(tmp_path, "main", "a" * 40)]
    assert result == {
        "state": "synced",
        "error": "",
        "path": str(tmp_path),
        "branch": "main",
        "head": "canonical-head",
        "merge_commit": "a" * 40,
        "synced_at": "2026-07-14T16:17:31Z",
        "ok": True,
    }


def test_agent_runtime_routes_the_tool_with_trusted_parent(monkeypatch):
    from agent.agent_runtime_helpers import invoke_tool
    from tools import canonical_checkout_sync_tool as sync_tool

    captured: dict[str, object] = {}

    def fake_sync_tool(**kwargs):
        captured.update(kwargs)
        return '{"ok": true}'

    monkeypatch.setattr(sync_tool, "sync_canonical_checkout_tool", fake_sync_tool)
    monkeypatch.setattr(
        "agent.tool_executor.apply_tool_result_hooks",
        lambda _name, _args, result, **_kwargs: result,
    )
    agent = SimpleNamespace(
        _context_engine_tool_names=set(),
        _memory_manager=None,
        session_id="session-1",
    )

    result = invoke_tool(
        agent,
        "sync_canonical_checkout",
        {"project_path": "/canonical", "branch": "main", "merge_commit": "a" * 40},
        "task-1",
    )

    assert result == '{"ok": true}'
    assert captured == {
        "project_path": "/canonical",
        "branch": "main",
        "merge_commit": "a" * 40,
        "parent_agent": agent,
    }
