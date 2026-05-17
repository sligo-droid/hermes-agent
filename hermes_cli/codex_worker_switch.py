"""Shared logic for the /codex-worker slash command.

The codex worker mode keeps Hermes on the normal tool-calling runtime, but
nudges coding-shaped requests toward the internal delegate_codex_coding_task
tool. That tool drives Codex through the app-server transport directly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class CodexWorkerStatus:
    success: bool
    enabled: bool
    old_enabled: Optional[bool] = None
    message: str = ""


def parse_args(arg_string: str) -> tuple[Optional[bool], list[str]]:
    """Return (new_enabled, errors). None means status-only."""
    raw = (arg_string or "").strip().lower()
    if not raw or raw == "status":
        return None, []
    if raw in ("on", "enable", "enabled", "true", "yes"):
        return True, []
    if raw in ("off", "disable", "disabled", "false", "no"):
        return False, []
    return None, ["Unknown codex-worker value {!r}. Use: on, off, status".format(raw)]


def get_enabled(config: dict) -> bool:
    """Read codex_worker.enabled. Defaults to True for new configs."""
    if not isinstance(config, dict):
        return True
    worker_cfg = config.get("codex_worker")
    if not isinstance(worker_cfg, dict):
        return True
    value = worker_cfg.get("enabled")
    if value is None:
        return True
    return bool(value)


def set_enabled(config: dict, enabled: bool) -> bool:
    old = get_enabled(config)
    if not isinstance(config.get("codex_worker"), dict):
        config["codex_worker"] = {}
    config["codex_worker"]["enabled"] = bool(enabled)
    return old


def apply(config: dict, new_enabled: Optional[bool], *, persist_callback=None) -> CodexWorkerStatus:
    current = get_enabled(config)
    if new_enabled is None:
        state = "on" if current else "off"
        return CodexWorkerStatus(
            success=True,
            enabled=current,
            old_enabled=current,
            message=(
                f"codex_worker.enabled: {state}\n"
                "Normal Hermes runtime will delegate coding-shaped requests "
                "to Codex when the delegate_codex_coding_task tool is available."
            ),
        )

    if new_enabled == current:
        state = "on" if current else "off"
        return CodexWorkerStatus(
            success=True,
            enabled=current,
            old_enabled=current,
            message=f"codex_worker.enabled already {state}",
        )

    old = set_enabled(config, new_enabled)
    if persist_callback is not None:
        try:
            persist_callback(config)
        except Exception as exc:
            return CodexWorkerStatus(
                success=False,
                enabled=new_enabled,
                old_enabled=old,
                message=f"updated config in memory but persist failed: {exc}",
            )

    old_state = "on" if old else "off"
    new_state = "on" if new_enabled else "off"
    return CodexWorkerStatus(
        success=True,
        enabled=new_enabled,
        old_enabled=old,
        message=f"codex_worker.enabled: {old_state} -> {new_state}",
    )


def looks_like_coding_request(message: str) -> bool:
    """Conservative heuristic for Codex worker guidance."""
    if not isinstance(message, str):
        return False
    text = message.strip()
    if not text:
        return False
    if text.startswith("[IMPORTANT: Background process "):
        return False

    lower = text.lower()
    explicit_phrases = (
        "use codex worker",
        "with codex worker",
        "delegate to codex",
        "codex coding task",
        "codex-worker",
    )
    if any(phrase in lower for phrase in explicit_phrases):
        return True

    if re.search(
        r"(^|[\s`'\"(])[\w./-]+\."
        r"(py|js|ts|tsx|jsx|rs|go|java|c|cc|cpp|h|hpp|rb|php|swift|kt|"
        r"scala|sh|yaml|yml|json|toml|md)"
        r"(?=$|[\s`'\"),:])",
        lower,
    ):
        return True

    coding_tokens = (
        "implement",
        "implementation",
        "refactor",
        "bug",
        "fix",
        "debug",
        "failing test",
        "failing tests",
        "test failure",
        "code review",
        "review this diff",
        "review this pr",
        "lint",
        "typecheck",
        "regression",
        "patch",
        "diff",
        "pull request",
        "coding",
    )
    for token in coding_tokens:
        if " " in token:
            if token in lower:
                return True
        elif re.search(rf"\b{re.escape(token)}\b", lower):
            return True
    return False


def build_worker_guidance(message: str, *, enabled: bool, tool_available: bool, api_mode: str) -> str:
    """Return an API-only user-message prefix, or empty string."""
    if not enabled or not tool_available:
        return ""
    if str(api_mode or "").strip().lower() == "codex_app_server":
        return ""
    if not looks_like_coding_request(message):
        return ""
    return (
        "[Codex worker mode is enabled. For implementation, debugging, "
        "test-fixing, refactor, and code-review requests, prefer "
        "`delegate_codex_coding_task` for the coding-heavy worker step when "
        "the task is concrete enough to hand off. Hermes remains the "
        "orchestrator: inspect context, prepare a bounded worker brief, review "
        "the changed files and tests after the worker returns, and report any "
        "blocker. If the request is not actually a coding task or Codex is a "
        "worse fit, continue with normal Hermes tools.]\n\n"
    )
