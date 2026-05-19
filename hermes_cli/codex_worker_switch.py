"""Shared logic for the /codex-worker slash command.

The codex worker mode keeps Hermes on the normal tool-calling runtime, but
nudges coding-shaped requests toward the internal delegate_codex_coding_task
tool. That tool drives Codex through the app-server transport directly.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class CodexWorkerStatus:
    success: bool
    enabled: bool
    old_enabled: Optional[bool] = None
    message: str = ""


@dataclass
class CodexWorkerRoutingDecision:
    guidance: str = ""
    required: bool = False
    should_delegate: bool = False
    fail_loud: bool = False
    hermes_context: bool = False
    coding_request: bool = False


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

    # If a platform/client echoes the API-only worker guidance back into a
    # later turn, ignore that synthetic block before classifying the user's
    # actual request. Otherwise the words "implementation, debugging, ..."
    # inside the guidance make every echoed turn look coding-shaped.
    if text.startswith(
        (
            "[Codex worker mode is enabled.",
            "[Hermes codebase coding request detected.",
        )
    ):
        end = text.find("]")
        if end != -1:
            text = text[end + 1 :].strip()
            if not text:
                return False

    lower = text.lower()

    # Meta/config/performance questions about the worker itself should stay in
    # Hermes-land. A bare mention of codex-worker is not a request to delegate.
    worker_terms = (
        "codex worker",
        "codex-worker",
        "delegate_codex_coding_task",
        "codex app-server",
        "codex app server",
        "openai_runtime",
    )
    meta_terms = (
        "why",
        "what",
        "how",
        "when",
        "where",
        "performance",
        "slow",
        "slower",
        "latency",
        "overhead",
        "responsible",
        "difference",
        "compare",
        "comparison",
        "benchmark",
        "test comparing",
        "turn on",
        "turn off",
        "enable",
        "disable",
        "enabled",
        "status",
        "config",
        "configuration",
        "runtime",
        "heuristic",
        "invoking",
        "routing",
    )
    if any(term in lower for term in worker_terms) and any(
        re.search(rf"\b{re.escape(term)}\b", lower)
        if term.isalpha()
        else term in lower
        for term in meta_terms
    ):
        return False

    explicit_phrases = (
        "use codex worker",
        "with codex worker",
        "delegate to codex",
        "codex coding task",
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

    test_phrases = (
        "write tests",
        "add tests",
        "test coverage",
        "unit test",
        "unit tests",
        "regression test",
        "regression tests",
    )
    if any(phrase in lower for phrase in test_phrases):
        return True

    action_re = (
        r"\b(?:add|build|create|update|change|modify|wire|hook up|integrate|"
        r"migrate|extend|replace|remove|support|make)\b"
    )
    anchor_re = (
        r"\b(?:component|page|route|endpoint|api|cli|command|config|loader|"
        r"adapter|schema|migration|dashboard|tui|ui|frontend|backend|gateway|"
        r"auth|login|session|state|worker|tool|plugin|provider|test|tests|"
        r"coverage)\b"
    )
    if re.search(action_re, lower) and re.search(anchor_re, lower):
        return True
    return False


def looks_like_hermes_coding_improvement_request(message: str) -> bool:
    """Hermes-only classifier for terse code-quality routing requests."""
    if not isinstance(message, str):
        return False
    lower = message.strip().lower()
    if not lower:
        return False

    action_re = (
        r"\b(?:tighten|improve|refine|harden|strengthen|adjust|tune|"
        r"update|change|fix)\b"
    )
    target_re = (
        r"\b(?:heuristic|heuristics|criteria|classifier|classification|"
        r"routing|route|guardrail|guardrails|gate|gates|detection|"
        r"decision|logic)\b"
    )
    return bool(re.search(action_re, lower) and re.search(target_re, lower))


def _inside_path(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except Exception:
        return False


def _known_hermes_roots() -> tuple[Path, ...]:
    home = Path.home()
    return (Path("/home/droid/hermes"), home / "hermes")


def _git_common_dir(cwd: Path) -> Optional[Path]:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=1.5,
            check=False,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    raw = proc.stdout.strip()
    if not raw:
        return None
    common = Path(raw).expanduser()
    if not common.is_absolute():
        common = cwd / common
    try:
        return common.resolve()
    except Exception:
        return common


def message_references_hermes_repo(message: str) -> bool:
    if not isinstance(message, str):
        return False
    lower = message.lower()
    if "/home/droid/hermes" in lower or "~/hermes" in lower:
        return True
    return bool(re.search(r"\bhermes\s+(?:repo|codebase)\b", lower))


def cwd_is_hermes_repo(cwd: Optional[str]) -> bool:
    if not cwd:
        return False
    try:
        cwd_path = Path(cwd).expanduser().resolve()
    except Exception:
        return False
    roots = _known_hermes_roots()
    if any(_inside_path(cwd_path, root) for root in roots):
        return True
    common = _git_common_dir(cwd_path)
    if common is None:
        return False
    return any(_inside_path(common, root / ".git") for root in roots)


def assess_worker_routing(
    message: str,
    *,
    enabled: bool,
    tool_available: bool,
    api_mode: str,
    cwd: Optional[str] = None,
) -> CodexWorkerRoutingDecision:
    """Classify whether this turn should receive Codex worker guidance."""
    mode = str(api_mode or "").strip().lower()
    if mode == "codex_app_server":
        return CodexWorkerRoutingDecision()

    hermes_context = cwd_is_hermes_repo(cwd) or message_references_hermes_repo(message)
    coding_request = looks_like_coding_request(message) or (
        hermes_context and looks_like_hermes_coding_improvement_request(message)
    )
    required = bool(coding_request and hermes_context)

    if required and not tool_available:
        return CodexWorkerRoutingDecision(
            guidance=(
                "[Hermes codebase coding request detected. This turn requires "
                "`delegate_codex_coding_task`, but that tool is unavailable. "
                "Do not implement with normal mutation tools; report this "
                "tool-availability blocker to the user.]\n\n"
            ),
            required=True,
            should_delegate=False,
            fail_loud=True,
            hermes_context=True,
            coding_request=True,
        )

    if required:
        return CodexWorkerRoutingDecision(
            guidance=(
                "[Hermes codebase coding request detected. You must call "
                "`delegate_codex_coding_task` for the coding-heavy worker step. "
                "Hermes remains the orchestrator: inspect enough context to "
                "prepare a bounded worker brief, then review the changed files "
                "and focused tests after the worker returns. Do not use direct "
                "mutation tools before the Codex worker has run.]\n\n"
            ),
            required=True,
            should_delegate=True,
            hermes_context=True,
            coding_request=True,
        )

    if not enabled or not tool_available or not coding_request:
        return CodexWorkerRoutingDecision(
            hermes_context=hermes_context,
            coding_request=coding_request,
        )
    return CodexWorkerRoutingDecision(
        guidance=(
            "[Codex worker mode is enabled. For implementation, debugging, "
            "test-fixing, refactor, and code-review requests, prefer "
            "`delegate_codex_coding_task` for the coding-heavy worker step when "
            "the task is concrete enough to hand off. Hermes remains the "
            "orchestrator: inspect context, prepare a bounded worker brief, review "
            "the changed files and tests after the worker returns, and report any "
            "blocker. If the request is not actually a coding task or Codex is a "
            "worse fit, continue with normal Hermes tools.]\n\n"
        ),
        should_delegate=True,
        hermes_context=hermes_context,
        coding_request=True,
    )


def build_worker_guidance(
    message: str,
    *,
    enabled: bool,
    tool_available: bool,
    api_mode: str,
    cwd: Optional[str] = None,
) -> str:
    """Return an API-only user-message prefix, or empty string."""
    decision = assess_worker_routing(
        message,
        enabled=enabled,
        tool_available=tool_available,
        api_mode=api_mode,
        cwd=cwd,
    )
    return decision.guidance


def requires_hermes_codex_worker(
    message: str,
    *,
    enabled: bool,
    api_mode: str,
    cwd: Optional[str] = None,
) -> bool:
    """Return True when direct mutation should wait for Codex delegation."""
    return assess_worker_routing(
        message,
        enabled=enabled,
        tool_available=True,
        api_mode=api_mode,
        cwd=cwd,
    ).required
