"""Deterministic trusted runtime phase classification."""

from __future__ import annotations

from typing import Any


_BROWSER_TOOLS = frozenset(
    {
        "browser",
        "browser_snapshot",
        "browser_click",
        "browser_type",
        "browser_scroll",
        "computer_use",
    }
)
_VISION_TOOLS = frozenset(
    {
        "visual_qa",
        "vision_analyze",
        "browser_vision",
        "browser_capture",
        "browser_screenshot",
    }
)
_CODING_WORKER_TOOLS = frozenset(
    {"delegate_coding_task", "coding_worker", "codex_worker"}
)
_CHECK_TOOLS = frozenset({"verification", "test", "tests", "lint", "type_check"})
_GIT_TOOLS = frozenset({"git", "git_status", "git_diff", "git_commit"})


def _token(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def classify_runtime_phase(operation: Any, *, tool_name: Any = "") -> str:
    """Map a trusted operation or tool name onto the runtime schema phases."""

    operation_token = _token(operation)
    tool_token = _token(tool_name)
    if (
        tool_token in _VISION_TOOLS
        or operation_token in {"visual_qa", "vision"}
        or operation_token in {"browser_vision", "browser_capture", "browser_screenshot"}
    ):
        return "vision"
    if (
        tool_token in _BROWSER_TOOLS
        or tool_token.startswith("browser_")
        or operation_token.startswith("browser_")
    ):
        return "browser"
    if tool_token in _CODING_WORKER_TOOLS or operation_token in {
        "coding_worker",
        "delegate_coding_task",
    }:
        return "coding_worker"
    if tool_token in _CHECK_TOOLS or operation_token in {"check", "verification", "test"}:
        return "check"
    if tool_token in _GIT_TOOLS or operation_token.startswith("git_"):
        return "git"
    if operation_token in {"model", "model_attempt", "model_retry", "llm_call"}:
        return "model"
    if operation_token.startswith("github_ci") or operation_token in {
        "ci",
        "ci_poll",
        "ci_check",
    }:
        return "ci"
    if operation_token.startswith("github_") or operation_token in {
        "github",
        "pull_request",
    }:
        return "github"
    if operation_token in {"review", "code_review", "review_reconciliation"}:
        return "review"
    if operation_token in {"deployment", "deploy", "deployment_poll"}:
        return "deployment"
    if operation_token in {
        "production_qa",
        "production_verification",
        "post_deploy_qa",
    }:
        return "production_qa"
    if operation_token in {"closeout", "trusted_closeout", "post_merge_closeout"}:
        return "closeout"
    if operation_token in {"canonical_sync", "canonical_checkout_sync"}:
        return "canonical_sync"
    if operation_token in {"restart", "gateway_restart", "service_restart"}:
        return "restart"
    if operation_token in {"gateway_handoff", "durable_handoff", "closeout_handoff"}:
        return "gateway_handoff"
    return "overhead"


__all__ = ["classify_runtime_phase"]
