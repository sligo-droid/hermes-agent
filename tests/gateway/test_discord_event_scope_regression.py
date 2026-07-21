"""Regression coverage for Discord gateway event-scoped metadata plumbing."""

from __future__ import annotations

import ast
from pathlib import Path


RUN_PY = Path(__file__).resolve().parents[2] / "gateway" / "run.py"
RUNNER_IMPLEMENTATION_CLASS = "_GatewayRunnerCore"


def _module_tree() -> ast.Module:
    return ast.parse(RUN_PY.read_text(encoding="utf-8"), filename=str(RUN_PY))


def _class_function(tree: ast.Module, class_name: str, function_name: str) -> ast.AsyncFunctionDef:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, ast.AsyncFunctionDef) and item.name == function_name:
                    return item
    raise AssertionError(f"{class_name}.{function_name} not found")


def _self_method_calls(function: ast.AsyncFunctionDef, method_name: str) -> list[ast.Call]:
    calls: list[ast.Call] = []
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == method_name
            and isinstance(func.value, ast.Name)
            and func.value.id == "self"
        ):
            calls.append(node)
    return calls


def _name_is_loaded(function: ast.AsyncFunctionDef, name: str) -> bool:
    return any(
        isinstance(node, ast.Name)
        and node.id == name
        and isinstance(node.ctx, ast.Load)
        for node in ast.walk(function)
    )


def test_run_agent_does_not_reference_message_event_outside_scope():
    """_run_agent is called with scalar event fields; it must not load `event`."""
    tree = _module_tree()
    run_agent = _class_function(tree, RUNNER_IMPLEMENTATION_CLASS, "_run_agent")

    args = [arg.arg for arg in run_agent.args.args + run_agent.args.kwonlyargs]
    assert "fable_plan_metadata" in args
    assert not _name_is_loaded(run_agent, "event")


def test_background_task_does_not_reference_message_event_outside_scope():
    """Background tasks do not carry MessageEvent; constructor kwargs must not load `event`."""
    tree = _module_tree()
    background_task = _class_function(
        tree,
        RUNNER_IMPLEMENTATION_CLASS,
        "_run_background_task",
    )

    assert not _name_is_loaded(background_task, "event")


def test_handle_message_forwards_fable_metadata_to_run_agent():
    """Fable metadata is the only event-scoped value _run_agent needs for fallback suppression."""
    tree = _module_tree()
    handle_message = _class_function(
        tree,
        RUNNER_IMPLEMENTATION_CLASS,
        "_handle_message_with_agent",
    )
    calls = _self_method_calls(handle_message, "_run_agent")

    assert calls, "_handle_message_with_agent should call self._run_agent"
    keywords = {kw.arg for call in calls for kw in call.keywords if kw.arg}
    assert "fable_plan_metadata" in keywords
