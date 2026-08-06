"""Tool-call execution — sequential and concurrent dispatch.

Both AIAgent methods (``_execute_tool_calls_sequential`` and
``_execute_tool_calls_concurrent``) live here as module-level
functions that take the parent ``AIAgent`` as their first argument.

``run_agent`` keeps thin wrappers so existing call sites work; tests
that patch ``run_agent._set_interrupt`` are honored because the
extracted functions reach back through the ``run_agent`` module via
``_ra()`` for that symbol.
"""

from __future__ import annotations

import concurrent.futures
import json
from pathlib import Path
import logging
import os
import queue
import random
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from agent.runtime_phase_classification import classify_runtime_phase
from agent.runtime_spans import finish_agent_runtime_span, start_agent_runtime_span
from agent.display import (
    KawaiiSpinner,
    build_tool_preview as _build_tool_preview,
    redact_tool_args_for_display as _redact_tool_args_for_display,
    get_cute_tool_message as _get_cute_tool_message_impl,
    get_tool_emoji as _get_tool_emoji,
    _detect_tool_failure,
)
from agent.tool_guardrails import ToolGuardrailDecision
from agent.preview_readiness import preview_block_result, record_preview_event
from agent.verification_evidence import (
    classify_tool_verification_evidence,
    classify_tool_visual_receipt,
)
from agent.tool_dispatch_helpers import (
    _is_destructive_command,
    _is_multimodal_tool_result,
    _multimodal_text_summary,
    _append_subdir_hint_to_multimodal,
    _plan_tool_batch_segments,
    make_tool_result_message,
)
from tools.terminal_tool import (
    get_active_env,
)
from tools.thread_context import propagate_context_to_thread
from tools.tool_result_storage import (
    maybe_persist_tool_result,
    enforce_turn_budget,
)
from tools.budget_config import BudgetConfig, DEFAULT_BUDGET, budget_for_context_window

logger = logging.getLogger(__name__)

_CLOSEOUT_FINALIZATION_REQUIRED = (
    "Closeout receipt accepted. All tools are now disabled for this turn; "
    "return one concise final response from the recorded evidence."
)
_CLOSEOUT_LOG_BUDGET = BudgetConfig(preview_size=0)
_PENDING_CLOSEOUT_VISUAL_TOOLS = frozenset(
    {
        "visual_qa",
        "browser_navigate",
        "browser_authenticate",
        "browser_snapshot",
        "browser_click",
        "browser_type",
        "browser_press",
        "browser_scroll",
        "browser_back",
    }
)


def _pending_closeout_allows_tool(function_name: str) -> bool:
    """Allow only visual inspection and bounded browser-state preparation."""

    return function_name in _PENDING_CLOSEOUT_VISUAL_TOOLS


def _reserve_visual_qa_call(agent: Any, function_name: str) -> bool:
    """Allow one initial visual check plus one correction retry per turn."""

    if function_name != "visual_qa":
        return True
    count = max(0, int(getattr(agent, "_visual_qa_tool_calls", 0) or 0))
    if count >= 2:
        return False
    agent._visual_qa_tool_calls = count + 1
    return True


def _append_visual_qa_limit_tool_result(
    agent: Any,
    messages: list,
    tool_call: Any,
) -> None:
    messages.append(
        make_tool_result_message(
            "visual_qa",
            (
                "[Tool execution skipped — visual_qa permits one initial call and one "
                "correction retry per turn. Continue from the recorded result.]"
            ),
            tool_call.id,
            effect_disposition="none",
        )
    )
    _flush_session_db_after_tool_progress(
        agent,
        messages,
        stage="visual QA retry limit tool result",
    )


def _closeout_receipt_gate_reason(agent: Any) -> str:
    if os.environ.get("HERMES_KANBAN_TASK"):
        return "kanban_terminal_required"
    work_id = str(getattr(agent, "_origin_work_item_id", "") or "").strip()
    if work_id:
        try:
            from gateway.work_ledger import GatewayWorkLedger

            state = GatewayWorkLedger().required_async_completion_state(work_id)
        except Exception:
            return "required_async_state_unavailable"
        if isinstance(state, dict) and state.get("has_required"):
            if state.get("failed"):
                return "required_async_failed"
            if int(state.get("required_pending_count") or 0) > 0 or not state.get("sealed"):
                return "required_async_pending"
    try:
        from agent.visual_qa import (
            normalize_visual_qa_config,
            normalize_visual_requirement,
            visual_receipt_completion,
        )

        config = normalize_visual_qa_config(getattr(agent, "visual_qa_config", None))
        requirement = normalize_visual_requirement(
            getattr(agent, "visual_qa_requirement", None)
        )
        if config["mode"] == "enforce_explicit" and requirement["level"] != "none":
            stats = getattr(agent, "_turn_runtime_stats", None)
            receipts = stats.get("visual_qa_receipts", []) if isinstance(stats, dict) else []
            completion = visual_receipt_completion(
                requirement,
                receipts,
                min_order=int(getattr(agent, "_visual_qa_last_edit_order", 0) or 0) + 1,
            )
            if completion.get("status") != "passed":
                return "visual_qa_pending"
    except Exception:
        return "visual_qa_state_unavailable"
    return ""


def _process_closeout_receipt(
    agent: Any,
    function_name: str,
    function_args: dict[str, Any] | None,
    result: Any,
) -> tuple[Any, bool]:
    """Accept one sanitized terminal closeout receipt into per-turn state."""

    if function_name != "terminal" or not isinstance(result, str):
        return result, False
    try:
        payload = json.loads(result)
    except (TypeError, ValueError, json.JSONDecodeError):
        return result, False
    if not isinstance(payload, dict):
        return result, False
    had_candidate = "closeout_receipt" in payload
    payload.pop("closeout_receipt", None)
    from agent.terminal_outcomes import (
        inspect_repo_closeout_receipt,
        sanitize_closeout_receipt,
    )

    args = function_args if isinstance(function_args, dict) else {}
    receipt = sanitize_closeout_receipt(
        inspect_repo_closeout_receipt(
            command=args.get("command"),
            cwd=args.get("workdir") or _current_session_cwd(agent),
            exit_code=payload.get("exit_code"),
            classification=payload.get("classification"),
            output=payload.get("output"),
        )
    )
    if receipt is None:
        if had_candidate:
            payload["closeout_receipt_rejected"] = {"reason": "invalid_receipt"}
            return json.dumps(payload, ensure_ascii=False), False
        return result, False
    reason = _closeout_receipt_gate_reason(agent)
    if reason == "visual_qa_pending":
        agent._pending_closeout_receipt = receipt
        agent._pending_closeout_cwd = str(
            args.get("workdir") or _current_session_cwd(agent)
        )
        agent._budget_grace_call = True
        payload = {
            "closeout_receipt": receipt,
            "closeout_receipt_pending": {
                "reason": reason,
                "required_tool": "visual_qa",
            },
        }
        return json.dumps(payload, ensure_ascii=False), False
    if reason:
        payload["closeout_receipt_rejected"] = {"reason": reason}
        return json.dumps(payload, ensure_ascii=False), False

    _accept_closeout_receipt(agent, receipt)
    payload = {
        "closeout_receipt": receipt,
        "finalization_required": _CLOSEOUT_FINALIZATION_REQUIRED,
    }
    return json.dumps(payload, ensure_ascii=False), True


def _accept_closeout_receipt(agent: Any, receipt: dict[str, Any]) -> None:
    """Promote one sanitized receipt to the turn's terminal state."""

    agent._pending_closeout_receipt = None
    agent._pending_closeout_cwd = None
    agent._accepted_closeout_receipt = receipt
    agent._closeout_finalization_attempts = 0
    agent._closeout_tool_choice_retries = 0
    agent._budget_grace_call = True
    stats = getattr(agent, "_turn_runtime_stats", None)
    if isinstance(stats, dict):
        stats["closeout_receipt"] = receipt


def _promote_pending_closeout_receipt(
    agent: Any,
    function_name: str,
    result: Any,
) -> tuple[Any, bool]:
    """Accept a pending closeout immediately after trusted visual QA passes."""

    receipt = getattr(agent, "_pending_closeout_receipt", None)
    if function_name != "visual_qa" or not isinstance(receipt, dict):
        return result, False
    if _closeout_receipt_gate_reason(agent):
        return result, False
    try:
        payload = json.loads(result) if isinstance(result, str) else None
    except (TypeError, ValueError, json.JSONDecodeError):
        return result, False
    if not isinstance(payload, dict):
        return result, False

    from agent.terminal_outcomes import closeout_receipt_matches_repo_state

    if not closeout_receipt_matches_repo_state(
        receipt,
        getattr(agent, "_pending_closeout_cwd", None),
    ):
        agent._pending_closeout_receipt = None
        agent._pending_closeout_cwd = None
        payload["closeout_receipt_rejected"] = {
            "reason": "repository_changed_after_closeout"
        }
        return json.dumps(payload, ensure_ascii=False), False

    _accept_closeout_receipt(agent, receipt)
    payload["closeout_receipt"] = receipt
    payload["finalization_required"] = _CLOSEOUT_FINALIZATION_REQUIRED
    return json.dumps(payload, ensure_ascii=False), True


def _attach_closeout_log_reference(
    compact_result: Any,
    full_result: Any,
    *,
    tool_call_id: str,
    effective_task_id: str,
) -> Any:
    """Persist a recognized closeout's full terminal result outside context."""

    if not isinstance(compact_result, str) or not isinstance(full_result, str):
        return compact_result
    try:
        payload = json.loads(compact_result)
    except (TypeError, ValueError, json.JSONDecodeError):
        return compact_result
    if not isinstance(payload, dict) or not isinstance(payload.get("closeout_receipt"), dict):
        return compact_result
    reference = maybe_persist_tool_result(
        content=full_result,
        tool_name="terminal",
        tool_use_id=tool_call_id,
        env=get_active_env(effective_task_id),
        config=_CLOSEOUT_LOG_BUDGET,
        threshold=0,
    )
    payload["closeout_log"] = reference
    return json.dumps(payload, ensure_ascii=False)


def _append_closeout_skipped_tool_results(
    agent: Any,
    messages: list,
    tool_calls: list,
) -> None:
    for tool_call in list(tool_calls or []):
        name = tool_call.function.name
        messages.append(
            make_tool_result_message(
                name,
                (
                    f"[Tool execution skipped — {name} was not started because an "
                    "authoritative closeout receipt already ended tool execution for this turn.]"
                ),
                tool_call.id,
                effect_disposition="none",
            )
        )
        _flush_session_db_after_tool_progress(
            agent,
            messages,
            stage=f"closeout-skipped tool result {name}",
        )


def _append_pending_closeout_skipped_tool_result(
    agent: Any,
    messages: list,
    tool_call: Any,
    *,
    tool_name: str | None = None,
) -> None:
    name = tool_name or tool_call.function.name
    messages.append(
        make_tool_result_message(
            name,
            (
                f"[Tool execution skipped — {name} was not started because a successful "
                "closeout is waiting for the required visual_qa check; only visual_qa "
                "and browser preparation tools may run.]"
            ),
            tool_call.id,
            effect_disposition="none",
        )
    )
    _flush_session_db_after_tool_progress(
        agent,
        messages,
        stage=f"pending-closeout-skipped tool result {name}",
    )


def _agent_has_pending_steer(agent) -> bool:
    checker = getattr(agent, "_has_pending_steer", None)
    if callable(checker):
        return bool(checker())
    return bool(getattr(agent, "_pending_steer", None))


def append_steer_skipped_tool_results(
    agent,
    messages: list,
    tool_calls: list,
    *,
    stage: str = "steer-skipped tool result",
    inject_guidance: bool = True,
) -> int:
    """Pair unstarted calls with synthetic results at a steer boundary."""
    calls = list(tool_calls or [])
    for tool_call in calls:
        name = tool_call.function.name
        messages.append(
            make_tool_result_message(
                name,
                (
                    f"[Tool execution skipped — {name} was not started because "
                    "new user guidance arrived. Replan before taking more actions.]"
                ),
                tool_call.id,
                effect_disposition="none",
            )
        )
        _flush_session_db_after_tool_progress(
            agent,
            messages,
            stage=f"{stage} {name}",
        )
    if calls and inject_guidance:
        agent._apply_pending_steer_to_tool_results(messages, len(calls))
    return len(calls)


def _storage_safe_tool_args(tool_name: str, args: dict) -> dict:
    """Return callback/persistence-safe args without changing execution args."""
    if not isinstance(args, dict):
        return args
    safe_args = args
    if "_parallel_group" in safe_args:
        safe_args = {
            key: value for key, value in safe_args.items()
            if key != "_parallel_group"
        }
    if tool_name == "browser_type" and "text" in safe_args:
        safe_args = {**safe_args, "text": "[REDACTED_BROWSER_INPUT]"}
    if tool_name == "visual_qa":
        try:
            from agent.visual_assertions import storage_safe_visual_qa_args

            safe_args = storage_safe_visual_qa_args(safe_args)
        except Exception:
            safe_args = {"assertions": []}
    return safe_args


def storage_safe_tool_calls(tool_calls: Any) -> list[dict[str, Any]] | None:
    """Return a durable copy of tool calls with sensitive arguments removed."""

    if not isinstance(tool_calls, list):
        return None
    safe_calls: list[dict[str, Any]] = []
    for raw in tool_calls:
        if isinstance(raw, dict):
            call = dict(raw)
            function = call.get("function")
            if isinstance(function, dict):
                safe_function = dict(function)
                name = str(safe_function.get("name") or "")
                arguments = safe_function.get("arguments")
                try:
                    parsed = json.loads(arguments) if isinstance(arguments, str) else arguments
                except (TypeError, ValueError, json.JSONDecodeError):
                    parsed = {}
                if isinstance(parsed, dict):
                    safe_parsed = _storage_safe_tool_args(name, parsed)
                    safe_function["arguments"] = json.dumps(
                        safe_parsed,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                call["function"] = safe_function
            elif "name" in call and "arguments" in call:
                name = str(call.get("name") or "")
                arguments = call.get("arguments")
                try:
                    parsed = json.loads(arguments) if isinstance(arguments, str) else arguments
                except (TypeError, ValueError, json.JSONDecodeError):
                    parsed = {}
                if isinstance(parsed, dict):
                    call["arguments"] = json.dumps(
                        _storage_safe_tool_args(name, parsed),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
            safe_calls.append(call)
            continue
        function = getattr(raw, "function", None)
        name = str(getattr(function, "name", "") or "")
        arguments = getattr(function, "arguments", "{}")
        try:
            parsed = json.loads(arguments) if isinstance(arguments, str) else arguments
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = {}
        safe_calls.append(
            {
                "name": name,
                "arguments": json.dumps(
                    _storage_safe_tool_args(name, parsed if isinstance(parsed, dict) else {}),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            }
        )
    return safe_calls


def _current_session_cwd(agent: Any = None) -> str:
    if agent is not None:
        cwd = getattr(agent, "session_cwd", None)
        if cwd:
            return str(cwd)
    try:
        from gateway.session_context import get_session_env

        cwd = get_session_env("HERMES_SESSION_CWD", "")
        if cwd:
            return cwd
    except Exception:
        pass
    return os.getenv("TERMINAL_CWD", os.getcwd())


def _visual_qa_required(agent: Any) -> bool:
    try:
        from agent.visual_qa import normalize_visual_requirement

        return normalize_visual_requirement(
            getattr(agent, "visual_qa_requirement", None)
        )["level"] in {"surface", "artifact"}
    except Exception:
        return False


def _preview_readiness_before_call(
    agent: Any,
    function_name: str,
    function_args: dict[str, Any],
) -> Any:
    controller = getattr(agent, "_preview_readiness", None)
    if controller is None:
        return None
    try:
        return controller.before_call(
            function_name,
            function_args,
            session_cwd=_current_session_cwd(agent),
            visual_required=_visual_qa_required(agent),
        )
    except Exception:
        logger.debug("preview readiness preflight failed", exc_info=True)
        return None


def _apply_preview_readiness_result(
    agent: Any,
    function_name: str,
    function_args: dict[str, Any],
    function_result: Any,
) -> Any:
    controller = getattr(agent, "_preview_readiness", None)
    if controller is None:
        return function_result
    try:
        function_result, event = controller.after_call(
            function_name,
            function_args,
            function_result,
            session_cwd=_current_session_cwd(agent),
            visual_required=_visual_qa_required(agent),
        )
        if event is not None:
            record_preview_event(getattr(agent, "_turn_runtime_stats", None), event)
        return function_result
    except Exception:
        logger.debug("preview readiness result classification failed", exc_info=True)
        return function_result


def _parallel_coding_base_cwd(agent: Any) -> str:
    """Resolve the coordinating worktree the same way a worker cwd is resolved."""
    path = Path(_current_session_cwd(agent)).expanduser()
    if not path.is_absolute():
        path = Path(os.getcwd()) / path
    return str(path.resolve(strict=False))


def _json_safe_parallel_merge_outcome(outcome: Any) -> Any:
    if isinstance(outcome, str):
        try:
            return json.loads(outcome)
        except (TypeError, ValueError):
            return outcome
    try:
        json.dumps(outcome)
        return outcome
    except (TypeError, ValueError):
        return str(outcome)


def _attach_parallel_merge_outcome(function_result: Any, outcome: Any) -> str:
    merge_payload = {"parallel_merge": _json_safe_parallel_merge_outcome(outcome)}
    if isinstance(function_result, str):
        try:
            payload = json.loads(function_result)
        except (TypeError, ValueError):
            payload = None
        if isinstance(payload, dict):
            payload.update(merge_payload)
            return json.dumps(payload, ensure_ascii=False)
        return f"{function_result.rstrip()}\n{json.dumps(merge_payload, ensure_ascii=False)}"
    return json.dumps(merge_payload, ensure_ascii=False)


def _merge_completed_parallel_coding_result(
    result_entry: Any,
    *,
    base_cwd: str,
    group_id: str,
) -> Any:
    """Merge one completed coding worker from the coordinating thread."""
    if result_entry is None:
        return None

    (
        function_name,
        function_args,
        storage_args,
        function_result,
        duration,
        is_error,
        blocked,
    ) = result_entry
    if function_name != "delegate_coding_task" or blocked:
        return result_entry

    try:
        payload = json.loads(function_result) if isinstance(function_result, str) else None
    except (TypeError, ValueError):
        payload = None
    parallel = payload.get("parallel") if isinstance(payload, dict) else None
    worker_cwd = parallel.get("worker_cwd") if isinstance(parallel, dict) else None

    if not isinstance(worker_cwd, str) or not worker_cwd.strip():
        merge_outcome = {
            "success": False,
            "recovery_required": True,
            "error": "Grouped coding-worker result did not include parallel.worker_cwd.",
            "next_action": (
                "Inspect the grouped worker result and recover its isolated worktree "
                "before continuing."
            ),
        }
    else:
        try:
            from tools.coding_worker_tool import merge_parallel_worker_result

            merge_outcome = merge_parallel_worker_result(
                base_cwd,
                worker_cwd,
                group_id,
            )
        except Exception as exc:
            logger.error(
                "parallel coding-worker merge-back failed for %s: %s",
                worker_cwd,
                exc,
                exc_info=True,
            )
            merge_outcome = {
                "success": False,
                "recovery_required": True,
                "error": f"Parallel coding-worker merge-back failed: {exc}",
                "next_action": (
                    "Inspect the isolated worker worktree and recover or retry the "
                    "merge-back before continuing."
                ),
            }

    function_result = _attach_parallel_merge_outcome(function_result, merge_outcome)
    normalized_outcome = _json_safe_parallel_merge_outcome(merge_outcome)
    merge_failed = bool(
        isinstance(normalized_outcome, dict)
        and (
            normalized_outcome.get("success") is False
            or normalized_outcome.get("recovery_required") is True
            or normalized_outcome.get("error")
        )
    )
    return (
        function_name,
        function_args,
        storage_args,
        function_result,
        duration,
        is_error or merge_failed,
        blocked,
    )


# Maximum number of concurrent worker threads for parallel tool execution.
# Mirrors the constant in ``run_agent`` for tests/imports that look here.
_MAX_TOOL_WORKERS = 8
_DEFAULT_CONCURRENT_TOOL_TIMEOUT_S = 420.0

_CODING_WORKER_BLOCKED_MUTATION_TOOLS = frozenset({
    "write_file",
    "patch",
    "execute_code",
    "sync_canonical_checkout",
})

_TERMINAL_MUTATION_PATTERNS = re.compile(
    r"""(?:^|\s|&&|\|\||;|`|\()(?:(?:apply_patch|touch|mkdir|ln|chmod|chown)\s|
        tee\s|
        git\s+(?:add|am|apply|branch\s+-D|cherry-pick|commit|merge|mv|pull|push|rebase|restore|revert|rm|stash|switch)\b|
        (?:npm|pnpm|yarn|bun|pip|uv|poetry)\s+(?:install|add|remove|update|upgrade|lock|sync)\b|
        (?:python\d*|node|ruby|perl)\s+(?:-|-[ce])(?:\s|$)
    )""",
    re.VERBOSE,
)

_READ_ONLY_BRIDGE_TOOLS = frozenset({"tool_describe", "tool_search"})



def _delegation_mutation_block(
    agent: Any,
    function_name: str,
    function_args: Optional[dict[str, Any]] = None,
) -> Optional[str]:
    """Enforce read-only/broker-only delegate policy after tool-search unwrap."""
    read_only = bool(getattr(agent, "_delegation_read_only", False))
    broker_only = bool(getattr(agent, "_delegation_broker_only_mutation", False))
    if not read_only and not broker_only:
        return None
    args = function_args or {}
    if read_only:
        return _read_only_runtime_block(agent, function_name, args)
    if broker_only and function_name == "request_coding_task":
        return None
    policy = "broker-only mutation"
    return (
        f"Blocked {function_name}: this delegated agent is in {policy} mode. "
        "Only explicit observation tools are allowed; terminal execution and "
        "unknown plugin/MCP tools fail closed."
    )


def _read_only_runtime_block(
    agent: Any,
    function_name: str,
    function_args: Optional[dict[str, Any]] = None,
) -> Optional[str]:
    """Fail closed on durable side effects for every read-only runtime."""
    from agent.runtime_capabilities import RuntimeMode, normalize_runtime_mode

    runtime_mode = normalize_runtime_mode(
        getattr(agent, "_runtime_mode", None),
        legacy_action_intent=(
            False if getattr(agent, "_discord_intake_read_only", False) else None
        ),
    )
    if (
        runtime_mode is not RuntimeMode.READ_ONLY
        and not getattr(agent, "_delegation_read_only", False)
    ):
        return None
    if function_name in _READ_ONLY_BRIDGE_TOOLS:
        return None
    from tools.registry import registry

    if function_name == "tool_call":
        try:
            from tools.tool_search import resolve_underlying_call

            underlying_name, underlying_args, error = resolve_underlying_call(
                function_args or {}
            )
        except Exception:
            underlying_name, underlying_args, error = None, {}, "bridge policy unavailable"
        if error or not underlying_name:
            return f"Blocked tool_call: {error or 'underlying tool is unavailable'}."
        block = registry.read_only_block(underlying_name, underlying_args)
        if block is None:
            return None
        return (
            f"{block} If this is observational, report the limitation and continue "
            "with available evidence; do not escalate merely to gain tool access. "
            "Escalate only if the user's original request requires durable change."
        )

    block = registry.read_only_block(function_name, function_args or {})
    if block is None:
        return None
    return (
        f"{block} If this is observational, report the limitation and continue "
        "with available evidence; do not escalate merely to gain tool access. "
        "Escalate only if the user's original request requires durable change."
    )


def _discord_intake_mutation_block(
    agent: Any,
    function_name: str,
    function_args: Optional[dict[str, Any]] = None,
) -> Optional[str]:
    """Compatibility alias for the former Discord-intake guard."""

    return _read_only_runtime_block(agent, function_name, function_args)


def _budget_for_agent(agent) -> BudgetConfig:
    try:
        context_length = getattr(
            getattr(agent, "context_compressor", None), "context_length", None
        )
        return (
            budget_for_context_window(int(context_length))
            if context_length
            else DEFAULT_BUDGET
        )
    except Exception:
        return DEFAULT_BUDGET


def _parse_tool_arguments(raw_arguments: Any) -> tuple[dict, Optional[str]]:
    try:
        arguments = json.loads(raw_arguments)
    except (json.JSONDecodeError, TypeError):
        arguments = None
    if isinstance(arguments, dict):
        return arguments, None
    return {}, json.dumps(
        {
            "error": "Invalid tool arguments",
            "message": "Tool arguments must be a valid JSON object; tool was not executed.",
        },
        ensure_ascii=False,
    )


def _resolve_concurrent_tool_timeout() -> float | None:
    raw = os.getenv("HERMES_CONCURRENT_TOOL_TIMEOUT_S", "").strip()
    if not raw:
        return _DEFAULT_CONCURRENT_TOOL_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "invalid HERMES_CONCURRENT_TOOL_TIMEOUT_S=%r; using %.0fs",
            raw,
            _DEFAULT_CONCURRENT_TOOL_TIMEOUT_S,
        )
        return _DEFAULT_CONCURRENT_TOOL_TIMEOUT_S
    return None if value <= 0 else value


def _flush_session_db_after_tool_progress(agent, messages: list, *, stage: str) -> None:
    try:
        agent._flush_messages_to_session_db(messages)
    except Exception as exc:
        logger.warning("Incremental tool-call persistence failed after %s: %s", stage, exc)


def _is_interpreter_shutdown_submit_error(exc: RuntimeError) -> bool:
    return "cannot schedule new futures after interpreter shutdown" in str(exc)


def _emit_terminal_post_tool_call(
    agent,
    *,
    function_name: str,
    function_args: dict,
    result: Any,
    effective_task_id: str,
    tool_call_id: str,
    duration_ms: int = 0,
    status: str | None = None,
    error_type: str | None = None,
    error_message: str | None = None,
    middleware_trace: Optional[list[dict[str, Any]]] = None,
) -> None:
    try:
        from model_tools import _emit_post_tool_call_hook

        _emit_post_tool_call_hook(
            function_name=function_name,
            function_args=function_args,
            result=result,
            task_id=effective_task_id or "",
            session_id=getattr(agent, "session_id", "") or "",
            tool_call_id=tool_call_id or "",
            turn_id=getattr(agent, "_current_turn_id", "") or "",
            api_request_id=getattr(agent, "_current_api_request_id", "") or "",
            duration_ms=duration_ms,
            status=status,
            error_type=error_type,
            error_message=error_message,
            middleware_trace=list(middleware_trace or []),
        )
    except Exception:
        pass


def _ra():
    """Lazy reference to ``run_agent`` so patches like ``run_agent._set_interrupt`` work."""
    import run_agent
    return run_agent


def _terminal_command_may_mutate(function_args: dict[str, Any]) -> bool:
    command = str(function_args.get("command") or "")
    if not command.strip():
        return False
    if _is_destructive_command(command):
        return True
    return bool(_TERMINAL_MUTATION_PATTERNS.search(command))


def _path_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except Exception:
        return False


def _known_hermes_roots_for_guard() -> tuple[Path, ...]:
    try:
        from hermes_cli.coding_worker_switch import _known_hermes_roots

        return tuple(_known_hermes_roots())
    except Exception:
        return (Path(__file__).resolve().parents[1],)


def _is_user_systemd_service_path(path: object) -> bool:
    raw = str(path or "").strip()
    if not raw:
        return False
    try:
        target = Path(raw).expanduser()
        if not target.is_absolute():
            return False
        target = target.resolve(strict=False)
        user_unit_dir = (Path.home() / ".config" / "systemd" / "user").resolve(strict=False)
    except Exception:
        return False

    if target.parent != user_unit_dir or target.suffix != ".service":
        return False
    return not any(_path_inside(target, root) for root in _known_hermes_roots_for_guard())


def _coding_worker_allows_host_service_mutation(function_name: str, function_args: dict[str, Any]) -> bool:
    if function_name == "write_file":
        return _is_user_systemd_service_path(function_args.get("path"))
    if function_name == "patch" and str(function_args.get("mode") or "replace") == "replace":
        return _is_user_systemd_service_path(function_args.get("path"))
    return False


def _coding_worker_mutation_block(agent, function_name: str, function_args: Optional[dict[str, Any]] = None) -> Optional[str]:
    """Return a guardrail message when Hermes-codebase work skipped the worker."""
    if function_name == "delegate_coding_task":
        return None
    function_args = function_args or {}
    mutation_tool = function_name in _CODING_WORKER_BLOCKED_MUTATION_TOOLS
    terminal_mutation = function_name == "terminal" and _terminal_command_may_mutate(function_args)
    if not mutation_tool and not terminal_mutation:
        return None
    if str(getattr(agent, "api_mode", "") or "").strip().lower() == "codex_app_server":
        return None
    if not getattr(agent, "_coding_worker_required_this_turn", False):
        return None
    if getattr(agent, "_coding_worker_used_this_turn", False):
        return None
    if _coding_worker_allows_host_service_mutation(function_name, function_args):
        return None
    return (
        "Hermes codebase coding requests must use delegate_coding_task "
        "before direct mutation tools. Call delegate_coding_task first; "
        "if that tool is unavailable, report that blocker instead of editing."
    )


def _coding_worker_result_attempted(result: object) -> bool:
    """Return True once delegate_coding_task actually returned a result."""
    return isinstance(result, str)


def _preflight_delegate_coding_task(function_args: dict[str, Any], agent: Any) -> tuple[dict[str, Any], str | None]:
    try:
        from tools.coding_worker_tool import preflight_delegate_coding_task

        preflight = preflight_delegate_coding_task(function_args, agent)
        return preflight.args, preflight.suppressed_result
    except Exception:
        logger.debug("delegate_coding_task preflight failed", exc_info=True)
        return dict(function_args or {}), None


def _record_turn_tool_runtime(
    agent: Any,
    function_name: str,
    duration: float,
    result: Any,
    is_error: bool,
    *,
    blocked: bool = False,
) -> None:
    stats = getattr(agent, "_turn_runtime_stats", None)
    if not isinstance(stats, dict):
        return
    try:
        duration_s = max(0.0, float(duration or 0.0))
        result_len = len(result) if isinstance(result, str) else len(str(result))
        stats["tool_calls"] = int(stats.get("tool_calls") or 0) + 1
        stats["tool_duration_s"] = float(stats.get("tool_duration_s") or 0.0) + duration_s
        stats["tool_chars"] = int(stats.get("tool_chars") or 0) + result_len
        if is_error:
            stats["tool_errors"] = int(stats.get("tool_errors") or 0) + 1
        if blocked:
            stats["tool_blocked"] = int(stats.get("tool_blocked") or 0) + 1
        tools = stats.setdefault("tools", {})
        item = tools.setdefault(
            function_name,
            {"count": 0, "duration_s": 0.0, "errors": 0, "blocked": 0, "chars": 0},
        )
        item["count"] = int(item.get("count") or 0) + 1
        item["duration_s"] = float(item.get("duration_s") or 0.0) + duration_s
        item["chars"] = int(item.get("chars") or 0) + result_len
        if is_error:
            item["errors"] = int(item.get("errors") or 0) + 1
        if blocked:
            item["blocked"] = int(item.get("blocked") or 0) + 1
    except Exception:
        logger.debug("turn tool runtime accounting failed", exc_info=True)


def _record_turn_verification_evidence(
    agent: Any,
    function_name: str,
    function_args: dict[str, Any] | None,
    result: Any,
    is_error: bool,
    duration_s: float = 0.0,
    *,
    visual_assertion_args: dict[str, Any] | None = None,
) -> None:
    stats = getattr(agent, "_turn_runtime_stats", None)
    if not isinstance(stats, dict):
        return
    try:
        order = int(stats.get("tool_calls") or 0)
        evidence = classify_tool_verification_evidence(
            function_name,
            function_args,
            result,
            is_error,
            order=order,
        )
        if evidence:
            trusted: dict[str, Any] = {}
            if function_name == "terminal" and not is_error:
                try:
                    result_data = result if isinstance(result, dict) else json.loads(str(result))
                except (TypeError, ValueError, json.JSONDecodeError):
                    result_data = {}
                candidate = (
                    result_data.get("verification_evidence")
                    if isinstance(result_data, dict)
                    else None
                )
                if isinstance(candidate, dict) and str(candidate.get("status") or "") == "passed":
                    repository_root = str(candidate.get("repository_root") or "").strip()
                    canonical_command = str(candidate.get("canonical_command") or "").strip()
                    scope = str(candidate.get("scope") or "").strip()
                    verified_head_sha = str(candidate.get("verified_head_sha") or "").strip().lower()
                    if repository_root and canonical_command and scope and re.fullmatch(
                        r"(?:[0-9a-f]{40}|[0-9a-f]{64})",
                        verified_head_sha,
                    ):
                        trusted = {
                            "repository_root": repository_root,
                            "canonical_command": canonical_command,
                            "scope": scope,
                            "mutation_generation": int(
                                getattr(agent, "_turn_mutation_generation", 0) or 0
                            ),
                            "mutation_boundary": int(
                                getattr(agent, "_turn_mutation_boundary", 0) or 0
                            ),
                            "verified_head_sha": verified_head_sha,
                        }
            if trusted:
                for item in evidence:
                    if (
                        isinstance(item, dict)
                        and str(item.get("status") or "") == "success"
                        and str(item.get("surface") or "") in {"verification", "ci"}
                    ):
                        item.update(trusted)
            stats.setdefault("verification_evidence", []).extend(evidence)
        # Visual QA is a distinct, opt-in receipt channel.  In particular, do
        # not derive it from ordinary browser evidence: navigation, snapshots,
        # screenshots, and console success cannot prove a visual assertion.
        requirement = getattr(agent, "visual_qa_requirement", None)
        receipt = classify_tool_visual_receipt(
            function_name,
            (
                visual_assertion_args
                if function_name == "visual_qa" and visual_assertion_args is not None
                else function_args
            ),
            result,
            is_error,
            order=order,
            requirement=requirement,
        )
        if receipt:
            try:
                from agent.visual_qa import normalize_visual_qa_config

                budget = normalize_visual_qa_config(
                    getattr(agent, "visual_qa_config", None)
                )["max_receipts_per_turn"]
            except Exception:
                budget = 1
            receipts = stats.setdefault("visual_qa_receipts", [])
            if isinstance(receipts, list) and budget > 0:
                receipts.append(receipt)
                receipts.sort(key=lambda item: int(item.get("order") or 0))
                del receipts[:-budget]
                try:
                    stats["visual_qa_check_duration_s"] = float(
                        stats.get("visual_qa_check_duration_s") or 0.0
                    ) + max(0.0, float(duration_s or 0.0))
                except (TypeError, ValueError):
                    stats["visual_qa_check_duration_s"] = 0.0
    except Exception:
        logger.debug("turn verification evidence accounting failed", exc_info=True)


def _record_visual_qa_edit_order(
    agent: Any,
    function_name: str,
    function_result: Any,
    *,
    task_id: str = "",
    tool_runtime_recorded: bool = False,
) -> None:
    """Remember the last landed edit's tool order for receipt freshness."""
    try:
        from agent.tool_result_classification import file_mutation_result_landed

        if not file_mutation_result_landed(function_name, function_result):
            return
        stats = getattr(agent, "_turn_runtime_stats", None)
        prior_calls = int(stats.get("tool_calls") or 0) if isinstance(stats, dict) else 0
        mutation_boundary = prior_calls if tool_runtime_recorded else prior_calls + 1
        agent._visual_qa_last_edit_order = mutation_boundary
        mutation_generation = int(getattr(agent, "_turn_mutation_generation", 0) or 0) + 1
        agent._turn_mutation_generation = mutation_generation
        agent._turn_mutation_boundary = mutation_boundary
        if isinstance(stats, dict):
            stats["mutation_generation"] = mutation_generation
            stats["mutation_boundary"] = mutation_boundary
        from tools.visual_assertion_runner import record_trusted_visual_mutation

        record_trusted_visual_mutation(task_id or getattr(agent, "_current_task_id", ""))
    except Exception:
        logger.debug("visual QA edit-order accounting failed", exc_info=True)


def _record_coding_worker_mutation_paths(
    agent: Any,
    function_name: str,
    function_result: Any,
) -> None:
    """Propagate host-inspected worker edits into the parent turn."""
    if function_name != "delegate_coding_task":
        return
    try:
        from agent.tool_result_classification import coding_worker_mutation_paths

        paths = coding_worker_mutation_paths(function_result)
        if not paths:
            return
        changed = getattr(agent, "_turn_file_mutation_paths", None)
        if changed is None:
            return
        changed.update(paths)
        from agent.visual_qa import (
            normalize_visual_qa_config,
            promote_visual_requirement_for_mutations,
            set_active_visual_requirement,
        )

        visual_config = normalize_visual_qa_config(
            getattr(agent, "visual_qa_config", None)
        )
        if visual_config["mode"] != "enforce_explicit":
            return
        promoted = promote_visual_requirement_for_mutations(
            getattr(agent, "visual_qa_requirement", None),
            changed,
            actionable=(
                str(getattr(agent, "_runtime_mode", "") or "").strip().lower()
                == "action"
            ),
        )
        agent.visual_qa_requirement = promoted
        set_active_visual_requirement(promoted)
        stats = getattr(agent, "_turn_runtime_stats", None)
        if isinstance(stats, dict):
            stats["visual_qa_level"] = promoted["level"]
    except Exception:
        logger.debug("coding-worker mutation accounting failed", exc_info=True)


def apply_tool_result_hooks(
    function_name: str,
    function_args: dict,
    function_result: Any,
    *,
    task_id: str = "",
    session_id: str = "",
    tool_call_id: str = "",
    duration_ms: int = 0,
    middleware_trace: list[dict[str, Any]] | None = None,
) -> Any:
    """Run observational and transform hooks for a completed tool result."""
    try:
        from hermes_cli.plugins import has_hook, invoke_hook

        if has_hook("post_tool_call"):
            invoke_hook(
                "post_tool_call",
                tool_name=function_name,
                args=function_args,
                result=function_result,
                task_id=task_id or "",
                session_id=session_id or "",
                tool_call_id=tool_call_id or "",
                turn_id="",
                api_request_id="",
                duration_ms=duration_ms,
                status="ok",
                error_type=None,
                error_message=None,
                middleware_trace=list(middleware_trace or []),
            )
    except Exception as _hook_err:
        logger.debug("post_tool_call hook error: %s", _hook_err)

    try:
        from hermes_cli.plugins import has_hook, invoke_hook

        if not has_hook("transform_tool_result"):
            return function_result
        hook_results = invoke_hook(
            "transform_tool_result",
            tool_name=function_name,
            args=function_args,
            result=function_result,
            task_id=task_id or "",
            session_id=session_id or "",
            tool_call_id=tool_call_id or "",
            turn_id="",
            api_request_id="",
            duration_ms=duration_ms,
            status="ok",
            error_type=None,
            error_message=None,
        )
        for hook_result in hook_results:
            if isinstance(hook_result, str):
                return hook_result
    except Exception as _hook_err:
        logger.debug("transform_tool_result hook error: %s", _hook_err)

    return function_result


def _tool_search_scoped_names(agent) -> frozenset:
    """Return the deferrable tool names the session may invoke via tool_call.

    The Tool Search unwrap dispatches the underlying tool directly, bypassing
    the bridge branch (and its scope check) in
    ``model_tools.handle_function_call``. To keep a restricted-toolset session
    (subagent, kanban worker, curated gateway session) from reaching tools it
    was never granted, the unwrap validates the underlying name against this
    set: the deferrable subset of the session's own enabled/disabled toolset
    scope.

    Result is cached on the agent and refreshed when the tool registry's
    generation changes (e.g. an MCP server reconnects), so the common case is
    a dict lookup, not a full tool-defs rebuild on every tool call.
    """
    try:
        import model_tools
        from tools import tool_search as _ts
        from tools.registry import registry as _registry
    except Exception:
        return frozenset()

    enabled = getattr(agent, "enabled_toolsets", None)
    disabled = getattr(agent, "disabled_toolsets", None)
    cache_key = (
        getattr(_registry, "_generation", 0),
        frozenset(enabled) if enabled is not None else None,
        frozenset(disabled) if disabled is not None else None,
        str(getattr(agent, "_runtime_mode", "") or ""),
    )
    cached = getattr(agent, "_tool_search_scope_cache", None)
    if cached is not None and cached[0] == cache_key:
        return cached[1]
    try:
        scoped_defs = model_tools.get_tool_definitions(
            enabled_toolsets=enabled,
            disabled_toolsets=disabled,
            quiet_mode=True,
            skip_tool_search_assembly=True,
            runtime_mode=getattr(agent, "_runtime_mode", None),
        ) or []
        names = _ts.scoped_deferrable_names(scoped_defs)
    except Exception:
        names = frozenset()
    try:
        agent._tool_search_scope_cache = (cache_key, names)
    except Exception:
        pass
    return names



def _apply_tool_request_middleware_for_agent(
    agent,
    *,
    function_name: str,
    function_args: dict,
    effective_task_id: str,
    tool_call_id: str,
) -> tuple[dict, list[dict[str, Any]]]:
    try:
        from hermes_cli.middleware import apply_tool_request_middleware

        result = apply_tool_request_middleware(
            function_name,
            function_args,
            task_id=effective_task_id or "",
            session_id=getattr(agent, "session_id", "") or "",
            tool_call_id=tool_call_id or "",
            turn_id=getattr(agent, "_current_turn_id", "") or "",
            api_request_id=getattr(agent, "_current_api_request_id", "") or "",
        )
        payload = result.payload if isinstance(result.payload, dict) else function_args
        return payload, list(result.trace)
    except Exception as exc:
        logger.debug("tool_request middleware error: %s", exc)
        return function_args, []


def _run_agent_tool_execution_middleware(
    agent,
    *,
    function_name: str,
    function_args: dict,
    effective_task_id: str,
    tool_call_id: str,
    execute,
) -> tuple[Any, dict]:
    observed_args = function_args

    def _execute(next_args: dict) -> Any:
        nonlocal observed_args
        observed_args = next_args if isinstance(next_args, dict) else function_args
        read_only_block = _read_only_runtime_block(
            agent, function_name, observed_args
        )
        if read_only_block is not None:
            return json.dumps({"error": read_only_block}, ensure_ascii=False)
        return execute(observed_args)

    from hermes_cli.middleware import run_tool_execution_middleware

    result = run_tool_execution_middleware(
        function_name,
        function_args,
        _execute,
        original_args=function_args,
        task_id=effective_task_id or "",
        session_id=getattr(agent, "session_id", "") or "",
        tool_call_id=tool_call_id or "",
        turn_id=getattr(agent, "_current_turn_id", "") or "",
        api_request_id=getattr(agent, "_current_api_request_id", "") or "",
    )
    return result, observed_args


def execute_tool_calls_concurrent(agent, assistant_message, messages: list, effective_task_id: str, api_call_count: int = 0, *, finalize: bool = True) -> None:
    """Execute multiple tool calls concurrently using a thread pool.

    Results are collected in the original tool-call order and appended to
    messages so the API sees them in the expected sequence.

    ``finalize=False`` skips the end-of-batch aggregate budget enforcement
    and /steer injection — used when this call is one segment of a larger
    mixed batch and the segmented dispatcher owns the turn-end work.
    """
    tool_calls = assistant_message.tool_calls
    num_tools = len(tool_calls)
    # Linearization point for the whole submitted parallel batch. Guidance
    # accepted before this check skips every call; guidance accepted after it
    # treats the batch as already submitted and lets it finish naturally.
    if _agent_has_pending_steer(agent):
        append_steer_skipped_tool_results(
            agent,
            messages,
            tool_calls,
            stage="pre-submit steer-skipped tool result",
        )
        rewrite_messages = getattr(agent, "_rewrite_messages_to_session_db", None)
        if callable(rewrite_messages):
            rewrite_messages(messages)
        return
    _tool_budget = _budget_for_agent(agent)

    # ── Pre-flight: interrupt check ──────────────────────────────────
    if agent._interrupt_requested:
        print(f"{agent.log_prefix}⚡ Interrupt: skipping {num_tools} tool call(s)")
        for tc in tool_calls:
            messages.append(make_tool_result_message(
                tc.function.name,
                f"[Tool execution cancelled — {tc.function.name} was skipped due to user interrupt]",
                tc.id,
                effect_disposition="none",
            ))
        return

    # ── Parse args + pre-execution bookkeeping ───────────────────────
    parsed_calls = []  # list of (tool_call, function_name, function_args)
    middleware_by_call_id: dict[str, list[dict[str, Any]]] = {}
    for tool_call in tool_calls:
        function_name = tool_call.function.name

        function_args, malformed_args_result = _parse_tool_arguments(
            tool_call.function.arguments
        )

        if malformed_args_result is not None:
            parsed_calls.append(
                (
                    tool_call,
                    function_name,
                    function_args,
                    {},
                    malformed_args_result,
                    False,
                )
            )
            continue

        # Reset nudge counters only for a structurally valid invocation.
        if function_name == "memory":
            agent._turns_since_memory = 0
        elif function_name == "skill_manage":
            agent._iters_since_skill = 0


        # ── Tool Search unwrap ────────────────────────────────────────
        # When the model invokes the tool_call bridge, peel it open so
        # every downstream check (checkpointing, guardrails, plugin
        # pre-tool-call hooks, the display/activity feed, the post-call
        # callback) sees the underlying tool — not the bridge. This is
        # the OpenClaw lesson: hooks must observe the real tool name.
        #
        # The original tool_call entry on ``tool_call.function`` is left
        # untouched so the conversation transcript and the matching
        # tool_call_id are preserved exactly as the model emitted them.
        #
        # Scope gate: the unwrap dispatches the underlying tool directly
        # (bypassing the bridge branch in handle_function_call and its
        # scope check), so we enforce session toolset scope HERE. A tool
        # the session was not granted is rejected before any checkpoint,
        # hook, or dispatch fires.
        _ts_scope_block = None
        try:
            from tools import tool_search as _ts
            if function_name == _ts.TOOL_CALL_NAME:
                _underlying, _underlying_args, _err = _ts.resolve_underlying_call(function_args)
                if not _err and _underlying:
                    if _underlying in _tool_search_scoped_names(agent):
                        function_name = _underlying
                        function_args = _underlying_args
                    else:
                        _ts_scope_block = json.dumps({
                            "error": (
                                f"'{_underlying}' is not available in this session. "
                                "Use tool_search to find tools you can call."
                            ),
                        }, ensure_ascii=False)
        except Exception:
            pass

        if (
            isinstance(getattr(agent, "_pending_closeout_receipt", None), dict)
            and not _pending_closeout_allows_tool(function_name)
        ):
            _append_pending_closeout_skipped_tool_result(
                agent,
                messages,
                tool_call,
                tool_name=function_name,
            )
            continue
        if not _reserve_visual_qa_call(agent, function_name):
            _append_visual_qa_limit_tool_result(agent, messages, tool_call)
            continue

        function_args, middleware_trace = _apply_tool_request_middleware_for_agent(
            agent,
            function_name=function_name,
            function_args=function_args,
            effective_task_id=effective_task_id,
            tool_call_id=getattr(tool_call, "id", "") or "",
        )
        middleware_by_call_id[str(getattr(tool_call, "id", "") or "")] = list(
            middleware_trace
        )

        # ── Block evaluation (BEFORE checkpoint preflight) ───────────
        # We must know whether the tool will execute before touching
        # checkpoint state (dedup slot, real snapshots).
        block_result = None
        blocked_by_guardrail = False
        if function_name == "delegate_coding_task":
            function_args, block_result = _preflight_delegate_coding_task(function_args, agent)
        if _ts_scope_block is not None:
            # Out-of-scope tool_call: reject before hooks/guardrails/dispatch.
            block_result = _ts_scope_block
            _emit_terminal_post_tool_call(
                agent,
                function_name=function_name,
                function_args=function_args,
                result=block_result,
                effective_task_id=effective_task_id,
                tool_call_id=getattr(tool_call, "id", "") or "",
                status="blocked",
                error_type="tool_scope_block",
                error_message=_ts_scope_block,
                middleware_trace=list(middleware_trace),
            )
        elif block_result is None:
            block_message = _delegation_mutation_block(agent, function_name, function_args)
            if block_message is None:
                block_message = _read_only_runtime_block(
                    agent, function_name, function_args
                )
            if block_message is None:
                block_message = _coding_worker_mutation_block(agent, function_name, function_args)
            if block_message is None:
                try:
                    from hermes_cli.plugins import resolve_pre_tool_block
                    block_message = resolve_pre_tool_block(
                        function_name,
                        function_args,
                        task_id=effective_task_id or "",
                        session_id=getattr(agent, "session_id", "") or "",
                        tool_call_id=getattr(tool_call, "id", "") or "",
                        turn_id=getattr(agent, "_current_turn_id", "") or "",
                        api_request_id=getattr(agent, "_current_api_request_id", "") or "",
                        middleware_trace=list(middleware_trace),
                    )
                except Exception:
                    block_message = None

            if block_message is not None:
                block_result = json.dumps({"error": block_message}, ensure_ascii=False)
            else:
                guardrail_decision = agent._tool_guardrails.before_call(function_name, function_args)
                if not guardrail_decision.allows_execution:
                    block_result = agent._guardrail_block_result(guardrail_decision)
                    blocked_by_guardrail = True

        # ── Checkpoint preflight (only for tools that will execute) ──
        if block_result is None:
            # Checkpoint for file-mutating tools
            if function_name in {"write_file", "patch"} and agent._checkpoint_mgr.enabled:
                try:
                    file_path = function_args.get("path", "")
                    if file_path:
                        work_dir = agent._checkpoint_mgr.get_working_dir_for_path(file_path)
                        agent._checkpoint_mgr.ensure_checkpoint(work_dir, f"before {function_name}")
                except Exception:
                    pass

            # Checkpoint before destructive terminal commands
            if function_name == "terminal" and agent._checkpoint_mgr.enabled:
                try:
                    cmd = function_args.get("command", "")
                    if _is_destructive_command(cmd):
                        cwd = function_args.get("workdir") or _current_session_cwd(agent)
                        agent._checkpoint_mgr.ensure_checkpoint(
                            cwd, f"before terminal: {cmd[:60]}"
                        )
                except Exception:
                    pass

        storage_args = _storage_safe_tool_args(function_name, function_args)
        parsed_calls.append((tool_call, function_name, function_args, storage_args, block_result, blocked_by_guardrail))

    # ── Logging / callbacks ──────────────────────────────────────────
    tool_names_str = ", ".join(name for _, name, _, _, _, _ in parsed_calls)
    if not agent.quiet_mode:
        print(f"  ⚡ Concurrent: {num_tools} tool calls — {tool_names_str}")
        for i, (tc, name, args, storage_args, block_result, blocked_by_guardrail) in enumerate(parsed_calls, 1):
            args_str = json.dumps(storage_args, ensure_ascii=False)
            if agent.verbose_logging:
                print(f"  📞 Tool {i}: {name}({list(storage_args.keys())})")
                print(agent._wrap_verbose("Args: ", json.dumps(storage_args, indent=2, ensure_ascii=False)))
            else:
                args_preview = args_str[:agent.log_prefix_chars] + "..." if len(args_str) > agent.log_prefix_chars else args_str
                print(f"  📞 Tool {i}: {name}({list(args.keys())}) - {args_preview}")

    for tc, name, args, storage_args, block_result, blocked_by_guardrail in parsed_calls:
        if block_result is not None:
            continue
        if agent.tool_progress_callback:
            try:
                preview = _build_tool_preview(name, storage_args)
                agent.tool_progress_callback("tool.started", name, preview, storage_args)
            except Exception as cb_err:
                logging.debug(f"Tool progress callback error: {cb_err}")

    for tc, name, args, storage_args, block_result, blocked_by_guardrail in parsed_calls:
        if block_result is not None:
            continue
        if agent.tool_start_callback:
            try:
                agent.tool_start_callback(tc.id, name, storage_args)
            except Exception as cb_err:
                logging.debug(f"Tool start callback error: {cb_err}")

    parallel_coding_group = None
    coding_batch_calls = [
        item for item in parsed_calls if item[1] == "delegate_coding_task"
    ]
    mixed_coding_batch = bool(coding_batch_calls) and len(coding_batch_calls) < len(parsed_calls)
    if len(coding_batch_calls) > 1 or mixed_coding_batch:
        parallel_base_cwd = _parallel_coding_base_cwd(agent)
        try:
            from tools.coding_worker_tool import _git_workspace_baseline

            parallel_base_sha, parallel_initial_dirty_paths = _git_workspace_baseline(
                parallel_base_cwd
            )
        except Exception:
            parallel_base_sha, parallel_initial_dirty_paths = "", []
        parallel_coding_group = {
            "group_id": uuid.uuid4().hex,
            "base_cwd": parallel_base_cwd,
            "base_sha": parallel_base_sha,
            "initial_dirty_paths": parallel_initial_dirty_paths,
        }
        for _tc, _name, args, _storage_args, _block_result, _blocked in coding_batch_calls:
            args["_parallel_group"] = dict(parallel_coding_group)

    # ── Concurrent execution ─────────────────────────────────────────
    concurrency_group_id = f"tool-batch-{uuid.uuid4().hex[:12]}"
    # Each slot holds (function_name, execution_args, storage_args, result, duration, error_flag, blocked_flag)
    results = [None] * num_tools
    for i, (tc, name, args, storage_args, block_result, blocked_by_guardrail) in enumerate(parsed_calls):
        if block_result is not None:
            results[i] = (name, args, storage_args, block_result, 0.0, True, True)
            blocked_span = start_agent_runtime_span(
                agent,
                name,
                phase=classify_runtime_phase("tool", tool_name=name),
                attempt_id=str(tc.id or ""),
                concurrency_id=concurrency_group_id,
                metadata={"tool": name},
            )
            finish_agent_runtime_span(agent, blocked_span, status="blocked")

    # Touch activity before launching workers so the gateway knows
    # we're executing tools (not stuck).
    agent._current_tool = tool_names_str
    agent._touch_activity(f"executing {num_tools} tools concurrently: {tool_names_str}")

    def _run_tool(index, tool_call, function_name, function_args):
        """Worker function executed in a thread."""
        middleware_trace = middleware_by_call_id.get(
            str(getattr(tool_call, "id", "") or ""), []
        )
        # Register this worker tid so the agent can fan out an interrupt
        # to it — see AIAgent.interrupt().  Must happen first thing, and
        # must be paired with discard + clear in the finally block.
        _worker_tid = threading.current_thread().ident
        with agent._tool_worker_threads_lock:
            agent._tool_worker_threads.add(_worker_tid)
        # Race: if the agent was interrupted between fan-out (which
        # snapshotted an empty/earlier set) and our registration, apply
        # the interrupt to our own tid now so is_interrupted() inside
        # the tool returns True on the next poll.
        if agent._interrupt_requested:
            try:
                _ra()._set_interrupt(True, _worker_tid)
            except Exception:
                pass
        # Set the activity callback on THIS worker thread so
        # _wait_for_process (terminal commands) can fire heartbeats.
        # The callback is thread-local; the main thread's callback
        # is invisible to worker threads.
        try:
            from tools.environments.base import set_activity_callback
            set_activity_callback(agent._touch_activity)
        except Exception:
            pass
        # Approval/sudo callbacks (thread-local) and the agent turn's
        # ContextVars are propagated by propagate_context_to_thread() at the
        # submit site below (GHSA-qg5c-hvr5-hjgr, #13617).
        start = time.time()
        runtime_span = start_agent_runtime_span(
            agent,
            function_name,
            phase=classify_runtime_phase("tool", tool_name=function_name),
            attempt_id=str(tool_call.id or ""),
            concurrency_id=concurrency_group_id,
            metadata={"tool": function_name},
        )
        span_status = "cancelled"
        try:
            try:
                result, observed_args = _run_agent_tool_execution_middleware(
                    agent,
                    function_name=function_name,
                    function_args=function_args,
                    effective_task_id=effective_task_id,
                    tool_call_id=getattr(tool_call, "id", "") or "",
                    execute=lambda next_args: agent._invoke_tool(
                        function_name,
                        next_args,
                        effective_task_id,
                        tool_call.id,
                        messages=messages,
                        pre_tool_block_checked=True,
                        skip_tool_request_middleware=True,
                        tool_request_middleware_trace=list(middleware_trace),
                    ),
                )
                function_args = observed_args
            except Exception as tool_error:
                result = f"Error executing tool '{function_name}': {tool_error}"
                logger.error("_invoke_tool raised for %s: %s", function_name, tool_error, exc_info=True)
            duration = time.time() - start
            is_error, _ = _detect_tool_failure(function_name, result)
            span_status = "error" if is_error else "ok"
            if is_error:
                logger.info("tool %s failed (%.2fs): %s", function_name, duration, result[:200])
            else:
                logger.info("tool %s completed (%.2fs, %d chars)", function_name, duration, len(result))
            results[index] = (
                function_name,
                function_args,
                _storage_safe_tool_args(function_name, function_args),
                result,
                duration,
                is_error,
                False,
            )
        finally:
            finish_agent_runtime_span(agent, runtime_span, status=span_status)
            # Tear down worker-tid tracking.  Clear any interrupt bit we may
            # have set so the next task scheduled onto this recycled tid
            # starts with a clean slate.  This MUST be in a finally block
            # because BaseException subclasses (CancelledError, KeyboardInterrupt)
            # bypass ``except Exception`` and would otherwise leak the tid
            # into _interrupted_threads, poisoning the recycled thread.
            with agent._tool_worker_threads_lock:
                agent._tool_worker_threads.discard(_worker_tid)
            try:
                _ra()._set_interrupt(False, _worker_tid)
            except Exception:
                pass

    # Start spinner for CLI mode (skip when TUI handles tool progress)
    spinner = None
    if agent._should_emit_quiet_tool_messages() and agent._should_start_quiet_spinner():
        face = random.choice(KawaiiSpinner.get_waiting_faces())
        spinner = KawaiiSpinner(f"{face} ⚡ running {num_tools} tools concurrently", spinner_type='dots', print_fn=agent._print_fn)
        spinner.start()

    try:
        runnable_calls = [
            (i, tc, name, args)
            for i, (tc, name, args, storage_args, block_result, blocked_by_guardrail) in enumerate(parsed_calls)
            if block_result is None
        ]
        futures = []
        timed_out_indices: set[int] = set()
        timeout_s = _resolve_concurrent_tool_timeout()
        deadline = time.monotonic() + timeout_s if timeout_s is not None else None
        if runnable_calls:
            from tools.daemon_pool import DaemonThreadPoolExecutor

            max_workers = min(len(runnable_calls), _MAX_TOOL_WORKERS)
            executor = DaemonThreadPoolExecutor(max_workers=max_workers)
            completed_futures: queue.Queue = queue.Queue()
            future_indexes = {}
            abandon_executor = False
            try:
                for submit_index, (i, tc, name, args) in enumerate(runnable_calls):
                    try:
                        future = executor.submit(
                            propagate_context_to_thread(_run_tool), i, tc, name, args
                        )
                    except RuntimeError as submit_error:
                        if not _is_interpreter_shutdown_submit_error(submit_error):
                            raise
                        skipped_calls = runnable_calls[submit_index:]
                        logger.warning(
                            "interpreter shutdown while scheduling concurrent tools; "
                            "skipping %d unsubmitted tool(s)",
                            len(skipped_calls),
                        )
                        for skipped_i, skipped_tc, skipped_name, skipped_args in skipped_calls:
                            if results[skipped_i] is not None:
                                continue
                            storage_args = parsed_calls[skipped_i][3]
                            skipped_result = (
                                f"Error executing tool '{skipped_name}': "
                                "Python interpreter is shutting down; tool was not started"
                            )
                            results[skipped_i] = (
                                skipped_name,
                                skipped_args,
                                storage_args,
                                skipped_result,
                                0.0,
                                True,
                                False,
                            )
                            _emit_terminal_post_tool_call(
                                agent,
                                function_name=skipped_name,
                                function_args=skipped_args,
                                result=skipped_result,
                                effective_task_id=effective_task_id,
                                tool_call_id=getattr(skipped_tc, "id", "") or "",
                                status="error",
                                error_type="interpreter_shutdown",
                                error_message=skipped_result,
                                middleware_trace=middleware_by_call_id.get(
                                    str(getattr(skipped_tc, "id", "") or ""), []
                                ),
                            )
                        break
                    futures.append(future)
                    future_indexes[future] = i
                    future.add_done_callback(completed_futures.put)

                pending_futures = set(futures)
                interrupt_logged = False
                concurrent_start = time.monotonic()

                def _process_completed_future(completed_future) -> None:
                    if completed_future not in pending_futures:
                        return
                    pending_futures.discard(completed_future)
                    try:
                        completed_future.result()
                    except concurrent.futures.CancelledError:
                        return
                    except BaseException as tool_error:
                        logger.error(
                            "concurrent tool worker raised: %s",
                            tool_error,
                            exc_info=True,
                        )
                    result_index = future_indexes.get(completed_future)
                    if (
                        result_index is not None
                        and parallel_coding_group is not None
                        and parsed_calls[result_index][1] == "delegate_coding_task"
                        and not parsed_calls[result_index][2].get("background")
                    ):
                        results[result_index] = _merge_completed_parallel_coding_result(
                            results[result_index],
                            base_cwd=parallel_coding_group["base_cwd"],
                            group_id=parallel_coding_group["group_id"],
                        )

                while pending_futures:
                    wait_for = 5.0
                    if deadline is not None:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            abandon_executor = True
                            timed_out_indices = {
                                future_indexes[f]
                                for f in pending_futures
                                if not f.done() and f in future_indexes
                            }
                            break
                        wait_for = min(wait_for, remaining)
                    try:
                        completed_future = completed_futures.get(timeout=wait_for)
                    except queue.Empty:
                        completed_future = None
                    if completed_future is not None:
                        _process_completed_future(completed_future)
                        continue
                    if agent._interrupt_requested:
                        abandon_executor = True
                        if not interrupt_logged:
                            interrupt_logged = True
                            agent._vprint(
                                f"{agent.log_prefix}⚡ Interrupt: cancelling "
                                f"{len(pending_futures)} pending concurrent tool(s)",
                                force=True,
                            )
                        break
                    elapsed = int(time.monotonic() - concurrent_start)
                    if elapsed > 0 and elapsed % 30 < 6:
                        still_running = [
                            parsed_calls[future_indexes[f]][1]
                            for f in pending_futures if f in future_indexes
                        ]
                        agent._touch_activity(
                            f"concurrent tools running ({elapsed}s, "
                            f"{len(pending_futures)} remaining: {', '.join(still_running[:3])})"
                        )

                if abandon_executor:
                    for future in pending_futures:
                        future.cancel()
                    with agent._tool_worker_threads_lock:
                        worker_tids = list(agent._tool_worker_threads)
                    for tid in worker_tids:
                        try:
                            _ra()._set_interrupt(True, tid)
                        except Exception:
                            pass
                    if timed_out_indices:
                        logger.warning(
                            "concurrent tool batch timed out after %.1fs; %d tool(s) still running",
                            timeout_s,
                            len(timed_out_indices),
                        )
                        for index in timed_out_indices:
                            if results[index] is None:
                                tc, name, args, storage_args, _block, _guard = parsed_calls[index]
                                timeout_result = (
                                    f"Error executing tool '{name}': timed out after {timeout_s:.1f}s"
                                )
                                results[index] = (
                                    name,
                                    args,
                                    storage_args,
                                    timeout_result,
                                    float(timeout_s or 0.0),
                                    True,
                                    False,
                                )
                                _emit_terminal_post_tool_call(
                                    agent,
                                    function_name=name,
                                    function_args=args,
                                    result=timeout_result,
                                    effective_task_id=effective_task_id,
                                    tool_call_id=getattr(tc, "id", "") or "",
                                    status="timeout",
                                    error_type="tool_timeout",
                                    error_message=timeout_result,
                                    middleware_trace=middleware_by_call_id.get(
                                        str(getattr(tc, "id", "") or ""), []
                                    ),
                                )
            finally:
                executor.shutdown(
                    wait=not abandon_executor,
                    cancel_futures=abandon_executor,
                )
    finally:
        if spinner:
            # Build a summary message for the spinner stop. Results are
            # (name, execution_args, storage_args, result, duration, is_error, blocked).
            completed = sum(1 for r in results if r is not None)
            total_dur = sum(r[4] for r in results if r is not None)
            spinner.stop(f"⚡ {completed}/{num_tools} tools completed in {total_dur:.1f}s total")

    # ── Post-execution: display per-tool results ─────────────────────
    for i, (tc, name, args, storage_args, block_result, blocked_by_guardrail) in enumerate(parsed_calls):
        r = results[i]
        blocked = False
        effect_disposition = "unknown" if i in timed_out_indices else None

        if r is None:
            # Tool was cancelled (interrupt) or thread didn't return
            if agent._interrupt_requested:
                function_result = f"[Tool execution cancelled — {name} was skipped due to user interrupt]"
            else:
                function_result = f"Error executing tool '{name}': thread did not return a result"
            tool_duration = 0.0
            is_error = True
        else:

            function_name, function_args, result_storage_args, function_result, tool_duration, is_error, blocked = r
            if blocked:
                effect_disposition = "none"

            if not blocked:
                if (
                    function_name == "delegate_coding_task"
                    and _coding_worker_result_attempted(function_result)
                ):
                    agent._coding_worker_used_this_turn = True

                function_result = agent._append_guardrail_observation(
                    function_name,
                    function_args,
                    function_result,
                    failed=is_error,
                )

            if is_error:
                _err_text = _multimodal_text_summary(function_result)
                result_preview = _err_text[:200] if len(_err_text) > 200 else _err_text
                logger.warning("Tool %s returned error (%.2fs): %s", function_name, tool_duration, result_preview)
            # Track file-mutation outcome for the turn-end verifier.
            # `blocked` calls never actually ran — don't let a guardrail
            # block count as either a failure or a success.
            if not blocked:
                try:
                    agent._record_file_mutation_result(
                        function_name, result_storage_args, function_result, is_error,
                    )
                    _record_coding_worker_mutation_paths(
                        agent,
                        function_name,
                        function_result,
                    )
                    _record_visual_qa_edit_order(
                        agent,
                        function_name,
                        function_result,
                        task_id=effective_task_id,
                    )
                except Exception as _ver_err:
                    logging.debug("file-mutation verifier record failed: %s", _ver_err)

            if not blocked and agent.tool_progress_callback:
                try:
                    agent.tool_progress_callback(
                        "tool.completed", function_name, None, None,
                        duration=tool_duration, is_error=is_error,
                        result=function_result,
                    )
                except Exception as cb_err:
                    logging.debug(f"Tool progress callback error: {cb_err}")

            if agent.verbose_logging:
                logging.debug(f"Tool {function_name} completed in {tool_duration:.2f}s")
                logging.debug(f"Tool result ({len(function_result)} chars): {function_result}")

        _record_turn_tool_runtime(
            agent,
            name,
            tool_duration,
            function_result,
            is_error,
            blocked=blocked,
        )
        if not blocked:
            _record_turn_verification_evidence(
                agent,
                name,
                storage_args,
                function_result,
                is_error,
                tool_duration,
                visual_assertion_args=args,
            )

        # Print cute message per tool
        if agent._should_emit_quiet_tool_messages():
            cute_msg = _get_cute_tool_message_impl(name, storage_args, tool_duration, result=function_result)
            agent._safe_print(f"  {cute_msg}")
        elif not agent.quiet_mode:
            _preview_str = _multimodal_text_summary(function_result)
            if agent.verbose_logging:
                print(f"  ✅ Tool {i+1} completed in {tool_duration:.2f}s")
                print(agent._wrap_verbose("Result: ", _preview_str))
            else:
                response_preview = _preview_str[:agent.log_prefix_chars] + "..." if len(_preview_str) > agent.log_prefix_chars else _preview_str
                print(f"  ✅ Tool {i+1} completed in {tool_duration:.2f}s - {response_preview}")

        agent._current_tool = None
        agent._touch_activity(f"tool completed: {name} ({tool_duration:.1f}s)")
        if not blocked and not is_error:
            try:
                agent._provider_no_progress_mark_progress(
                    "successful_tool_call", phase="tool_execution"
                )
            except Exception:
                pass

        if not blocked and agent.tool_complete_callback:
            try:
                agent.tool_complete_callback(tc.id, name, storage_args, function_result)
            except Exception as cb_err:
                logging.debug(f"Tool complete callback error: {cb_err}")

        function_result = maybe_persist_tool_result(
            content=function_result,
            tool_name=name,
            tool_use_id=tc.id,
            env=get_active_env(effective_task_id),
            config=_tool_budget,
        ) if not _is_multimodal_tool_result(function_result) else function_result

        subdir_hints = agent._subdirectory_hints.check_tool_call(name, storage_args)
        if subdir_hints:
            if _is_multimodal_tool_result(function_result):
                # Append the hint to the text summary part so the model
                # still sees it; don't touch the image blocks.
                _append_subdir_hint_to_multimodal(function_result, subdir_hints)
            else:
                function_result += subdir_hints

        # Unwrap _multimodal dicts to an OpenAI-style content list so any
        # vision-capable provider receives [{type:text},{type:image_url}]
        # rather than a raw Python dict.  The Anthropic adapter already
        # accepts content lists; vision-capable OpenAI-compatible servers
        # (mlx-vlm, GPT-4o, …) accept image_url in tool messages natively.
        # Text-only servers get a string-safe fallback here so a rejected
        # image tool result never poisons canonical session history.
        # String results pass through unchanged.
        _tool_content = agent._tool_result_content_for_active_model(name, function_result)

        tool_message = make_tool_result_message(
            name,
            _tool_content,
            tc.id,
            effect_disposition=effect_disposition,
        )
        messages.append(tool_message)
        risk_metadata = tool_message.get("_tool_output_risk")
        if (
            risk_metadata is not None
            and risk_metadata.get("risk") != "low"
            and agent.tool_progress_callback
        ):
            try:
                agent.tool_progress_callback(
                    "tool.output_risk",
                    name,
                    None,
                    None,
                    tool_call_id=tc.id,
                    risk_metadata=risk_metadata,
                )
            except Exception as cb_err:
                logging.debug("Tool output risk callback error: %s", cb_err)
        _flush_session_db_after_tool_progress(
            agent,
            messages,
            stage=f"tool result {name}",
        )

    # ── Per-turn aggregate budget enforcement ─────────────────────────
    num_tools = len(parsed_calls)
    if finalize and num_tools > 0:
        turn_tool_msgs = messages[-num_tools:]
        enforce_turn_budget(
            turn_tool_msgs,
            env=get_active_env(effective_task_id),
            config=_tool_budget,
        )

    # ── /steer injection ──────────────────────────────────────────────
    # Append any pending user steer text to the last tool result so the
    # agent sees it on its next iteration. Runs AFTER budget enforcement
    # so the steer marker is never truncated. See steer() for details.
    if finalize and num_tools > 0:
        agent._apply_pending_steer_to_tool_results(messages, num_tools)
        rewrite_messages = getattr(agent, "_rewrite_messages_to_session_db", None)
        if callable(rewrite_messages):
            rewrite_messages(messages)




def execute_tool_calls_sequential(agent, assistant_message, messages: list, effective_task_id: str, api_call_count: int = 0, *, finalize: bool = True) -> bool:
    """Execute tool calls sequentially (original behavior). Used for single calls or interactive tools.

    ``finalize=False`` skips the end-of-batch aggregate budget enforcement
    and /steer injection — used when this call is one segment of a larger
    mixed batch and the segmented dispatcher owns the turn-end work.
    """
    # Resolve the context-scaled tool-output budget once per turn.
    _tool_budget = _budget_for_agent(agent)
    steer_boundary_hit = False
    closeout_boundary_hit = False
    for i, tool_call in enumerate(assistant_message.tool_calls, 1):
        if getattr(agent, "_accepted_closeout_receipt", None):
            _append_closeout_skipped_tool_results(
                agent,
                messages,
                assistant_message.tool_calls[i - 1 :],
            )
            return True
        # Linearization point for this sequential call. Guidance accepted
        # before the check skips this and all later calls; guidance accepted
        # after it lets this one active call finish, then stops the batch.
        if _agent_has_pending_steer(agent):
            append_steer_skipped_tool_results(
                agent,
                messages,
                assistant_message.tool_calls[i - 1:],
                stage="pre-execution steer-skipped tool result",
            )
            rewrite_messages = getattr(agent, "_rewrite_messages_to_session_db", None)
            if callable(rewrite_messages):
                rewrite_messages(messages)
            return True
        # SAFETY: check interrupt BEFORE starting each tool.
        # If the user sent "stop" during a previous tool's execution,
        # do NOT start any more tools -- skip them all immediately.
        if agent._interrupt_requested:
            remaining_calls = assistant_message.tool_calls[i-1:]
            if remaining_calls:
                agent._vprint(f"{agent.log_prefix}⚡ Interrupt: skipping {len(remaining_calls)} tool call(s)", force=True)
            for skipped_tc in remaining_calls:
                skipped_name = skipped_tc.function.name

                messages.append(make_tool_result_message(
                    skipped_name,
                    f"[Tool execution cancelled — {skipped_name} was skipped due to user interrupt]",
                    skipped_tc.id,
                    effect_disposition="none",
                ))
                _flush_session_db_after_tool_progress(
                    agent,
                    messages,
                    stage=f"cancelled tool result {skipped_name}",
                )
            break

        function_name = tool_call.function.name

        function_args, malformed_args_result = _parse_tool_arguments(
            tool_call.function.arguments
        )
        if malformed_args_result is not None:
            messages.append(
                make_tool_result_message(
                    function_name,
                    malformed_args_result,
                    tool_call.id,
                )
            )
            _flush_session_db_after_tool_progress(
                agent,
                messages,
                stage=f"invalid tool arguments {function_name}",
            )
            if _agent_has_pending_steer(agent) and i < len(assistant_message.tool_calls):
                append_steer_skipped_tool_results(
                    agent,
                    messages,
                    assistant_message.tool_calls[i:],
                )
                steer_boundary_hit = True
                break
            continue

        # Tool Search unwrap — see execute_tool_calls_concurrent for full
        # rationale, including the scope gate (the unwrap dispatches the
        # underlying tool directly, so session toolset scope is enforced here).
        _ts_scope_block: Optional[str] = None
        try:
            from tools import tool_search as _ts
            if function_name == _ts.TOOL_CALL_NAME:
                _underlying, _underlying_args, _err = _ts.resolve_underlying_call(function_args)
                if not _err and _underlying:
                    if _underlying in _tool_search_scoped_names(agent):
                        function_name = _underlying
                        function_args = _underlying_args
                    else:
                        _ts_scope_block = (
                            f"'{_underlying}' is not available in this session. "
                            "Use tool_search to find tools you can call."
                        )
        except Exception:
            pass

        if (
            isinstance(getattr(agent, "_pending_closeout_receipt", None), dict)
            and not _pending_closeout_allows_tool(function_name)
        ):
            _append_pending_closeout_skipped_tool_result(
                agent,
                messages,
                tool_call,
                tool_name=function_name,
            )
            continue
        if not _reserve_visual_qa_call(agent, function_name):
            _append_visual_qa_limit_tool_result(agent, messages, tool_call)
            continue

        function_args, middleware_trace = _apply_tool_request_middleware_for_agent(
            agent,
            function_name=function_name,
            function_args=function_args,
            effective_task_id=effective_task_id,
            tool_call_id=getattr(tool_call, "id", "") or "",
        )
        _preflight_block_result: str | None = None
        if function_name == "delegate_coding_task":
            function_args, _preflight_block_result = _preflight_delegate_coding_task(function_args, agent)
        storage_args = _storage_safe_tool_args(function_name, function_args)

        # Check plugin hooks for a block directive before executing.
        _block_msg: Optional[str] = None
        if _preflight_block_result is not None:
            pass
        elif _ts_scope_block is not None:
            _block_msg = _ts_scope_block
        else:
            _block_msg = _delegation_mutation_block(agent, function_name, function_args)
            if _block_msg is None:
                _block_msg = _read_only_runtime_block(
                    agent, function_name, function_args
                )
            if _block_msg is None:
                _block_msg = _coding_worker_mutation_block(agent, function_name, function_args)
            if _block_msg is None:
                try:
                    from hermes_cli.plugins import resolve_pre_tool_block
                    _block_msg = resolve_pre_tool_block(
                        function_name,
                        function_args,
                        task_id=effective_task_id or "",
                        session_id=getattr(agent, "session_id", "") or "",
                        tool_call_id=getattr(tool_call, "id", "") or "",
                        turn_id=getattr(agent, "_current_turn_id", "") or "",
                        api_request_id=getattr(agent, "_current_api_request_id", "") or "",
                        middleware_trace=list(middleware_trace),
                    )
                except Exception:
                    pass

        _preview_block_decision = None
        if _block_msg is None:
            _preview_block_decision = _preview_readiness_before_call(
                agent, function_name, function_args
            )

        _guardrail_block_decision: ToolGuardrailDecision | None = None
        if _block_msg is None and _preview_block_decision is None:
            guardrail_decision = agent._tool_guardrails.before_call(function_name, function_args)
            if not guardrail_decision.allows_execution:
                _guardrail_block_decision = guardrail_decision

        _execution_blocked = (
            _preflight_block_result is not None
            or _block_msg is not None
            or _preview_block_decision is not None
            or _guardrail_block_decision is not None
        )

        if _execution_blocked:
            # Tool blocked by plugin or guardrail policy — skip counters,
            # callbacks, checkpointing, activity mutation, and real execution.
            pass
        # Reset nudge counters when the relevant tool is actually used
        elif function_name == "memory":
            agent._turns_since_memory = 0
        elif function_name == "skill_manage":
            agent._iters_since_skill = 0

        if not agent.quiet_mode:
            args_str = json.dumps(storage_args, ensure_ascii=False)
            if agent.verbose_logging:
                print(f"  📞 Tool {i}: {function_name}({list(storage_args.keys())})")
                print(agent._wrap_verbose("Args: ", json.dumps(storage_args, indent=2, ensure_ascii=False)))
            else:
                args_preview = args_str[:agent.log_prefix_chars] + "..." if len(args_str) > agent.log_prefix_chars else args_str
                print(f"  📞 Tool {i}: {function_name}({list(function_args.keys())}) - {args_preview}")

        if not _execution_blocked:
            agent._current_tool = function_name
            agent._touch_activity(f"executing tool: {function_name}")

        # Set activity callback for long-running tool execution (terminal
        # commands, etc.) so the gateway's inactivity monitor doesn't kill
        # the agent while a command is running.
        if not _execution_blocked:
            try:
                from tools.environments.base import set_activity_callback
                set_activity_callback(agent._touch_activity)
            except Exception:
                pass

        if not _execution_blocked and agent.tool_progress_callback:
            try:
                preview = _build_tool_preview(function_name, storage_args)
                agent.tool_progress_callback("tool.started", function_name, preview, storage_args)
            except Exception as cb_err:
                logging.debug(f"Tool progress callback error: {cb_err}")

        if not _execution_blocked and agent.tool_start_callback:
            try:
                agent.tool_start_callback(tool_call.id, function_name, storage_args)
            except Exception as cb_err:
                logging.debug(f"Tool start callback error: {cb_err}")

        # Checkpoint: snapshot working dir before file-mutating tools
        if not _execution_blocked and function_name in {"write_file", "patch"} and agent._checkpoint_mgr.enabled:
            try:
                file_path = function_args.get("path", "")
                if file_path:
                    work_dir = agent._checkpoint_mgr.get_working_dir_for_path(file_path)
                    agent._checkpoint_mgr.ensure_checkpoint(
                        work_dir, f"before {function_name}"
                    )
            except Exception:
                pass  # never block tool execution

        # Checkpoint before destructive terminal commands
        if not _execution_blocked and function_name == "terminal" and agent._checkpoint_mgr.enabled:
            try:
                cmd = function_args.get("command", "")
                if _is_destructive_command(cmd):
                    cwd = function_args.get("workdir") or _current_session_cwd(agent)
                    agent._checkpoint_mgr.ensure_checkpoint(
                        cwd, f"before terminal: {cmd[:60]}"
                    )
            except Exception:
                pass  # never block tool execution

        tool_start_time = time.time()
        runtime_span = start_agent_runtime_span(
            agent,
            function_name,
            phase=classify_runtime_phase("tool", tool_name=function_name),
            attempt_id=str(tool_call.id or ""),
            metadata={"tool": function_name},
        )
        _hooks_applied_by_dispatch = False

        if _preflight_block_result is not None:
            function_result = _preflight_block_result
            tool_duration = 0.0
            _emit_terminal_post_tool_call(
                agent,
                function_name=function_name,
                function_args=function_args,
                result=function_result,
                effective_task_id=effective_task_id,
                tool_call_id=getattr(tool_call, "id", "") or "",
                status="blocked",
                error_type="delegate_preflight_block",
                error_message=function_result,
                middleware_trace=list(middleware_trace),
            )
        elif _block_msg is not None:
            # Tool blocked by plugin policy — return error without executing.
            function_result = json.dumps({"error": _block_msg}, ensure_ascii=False)
            tool_duration = 0.0
            _emit_terminal_post_tool_call(
                agent,
                function_name=function_name,
                function_args=function_args,
                result=function_result,
                effective_task_id=effective_task_id,
                tool_call_id=getattr(tool_call, "id", "") or "",
                status="blocked",
                error_type=("tool_scope_block" if _ts_scope_block is not None else "plugin_block"),
                error_message=_block_msg,
                middleware_trace=list(middleware_trace),
            )
        elif _preview_block_decision is not None:
            function_result = preview_block_result(_preview_block_decision)
            record_preview_event(
                getattr(agent, "_turn_runtime_stats", None),
                _preview_block_decision.evidence,
            )
            tool_duration = 0.0
            _emit_terminal_post_tool_call(
                agent,
                function_name=function_name,
                function_args=function_args,
                result=function_result,
                effective_task_id=effective_task_id,
                tool_call_id=getattr(tool_call, "id", "") or "",
                status="blocked",
                error_type="preview_readiness_block",
                error_message=_preview_block_decision.message,
                middleware_trace=list(middleware_trace),
            )
        elif _guardrail_block_decision is not None:
            # Tool blocked by tool-loop guardrail — synthesize exactly one
            # tool result for the original tool_call_id without executing.
            function_result = agent._guardrail_block_result(_guardrail_block_decision)
            tool_duration = 0.0
            _emit_terminal_post_tool_call(
                agent,
                function_name=function_name,
                function_args=function_args,
                result=function_result,
                effective_task_id=effective_task_id,
                tool_call_id=getattr(tool_call, "id", "") or "",
                status="blocked",
                error_type="guardrail_block",
                error_message=(
                    getattr(_guardrail_block_decision, "message", None)
                    or "Tool blocked by guardrail policy"
                ),
                middleware_trace=list(middleware_trace),
            )
        elif function_name == "todo":
            def _execute(next_args: dict) -> Any:
                from tools.todo_tool import todo_tool as _todo_tool
                return _todo_tool(
                    todos=next_args.get("todos"),
                    merge=next_args.get("merge", False),
                    store=agent._todo_store,
                )

            function_result, function_args = _run_agent_tool_execution_middleware(
                agent,
                function_name=function_name,
                function_args=function_args,
                effective_task_id=effective_task_id,
                tool_call_id=getattr(tool_call, "id", "") or "",
                execute=_execute,
            )
            tool_duration = time.time() - tool_start_time
            if agent._should_emit_quiet_tool_messages():
                agent._vprint(f"  {_get_cute_tool_message_impl('todo', function_args, tool_duration, result=function_result)}")
        elif function_name == "session_search":
            session_db = agent._get_session_db_for_recall()
            if not session_db:
                from hermes_state import format_session_db_unavailable
                function_result = json.dumps({"success": False, "error": format_session_db_unavailable()})
            else:
                from tools.session_search_tool import session_search as _session_search
                function_result = _session_search(
                    query=function_args.get("query", ""),
                    role_filter=function_args.get("role_filter"),
                    limit=function_args.get("limit", 3),
                    session_id=function_args.get("session_id"),
                    around_message_id=function_args.get("around_message_id"),
                    window=function_args.get("window", 5),
                    sort=function_args.get("sort"),
                    db=session_db,
                    current_session_id=agent.session_id,
                )
            tool_duration = time.time() - tool_start_time
            if agent._should_emit_quiet_tool_messages():
                agent._vprint(f"  {_get_cute_tool_message_impl('session_search', function_args, tool_duration, result=function_result)}")
        elif function_name == "memory":
            target = function_args.get("target", "memory")
            if getattr(agent, "memory_read_only", False):
                function_result = json.dumps({"error": "The memory tool is disabled in read-only memory mode"})
            else:
                from tools.memory_tool import memory_tool as _memory_tool
                function_result = _memory_tool(
                    action=function_args.get("action"),
                    target=target,
                    content=function_args.get("content"),
                    old_text=function_args.get("old_text"),
                    store=agent._memory_store,
                )
                # Bridge: notify external memory provider of built-in memory writes
                if agent._memory_manager and function_args.get("action") in {"add", "replace"}:
                    try:
                        agent._memory_manager.on_memory_write(
                            function_args.get("action", ""),
                            target,
                            function_args.get("content", ""),
                            metadata=agent._build_memory_write_metadata(
                                task_id=effective_task_id,
                                tool_call_id=getattr(tool_call, "id", None),
                            ),
                        )
                    except Exception:
                        pass
            tool_duration = time.time() - tool_start_time
            if agent._should_emit_quiet_tool_messages():
                agent._vprint(f"  {_get_cute_tool_message_impl('memory', function_args, tool_duration, result=function_result)}")
        elif function_name == "clarify":
            from tools.clarify_tool import clarify_tool as _clarify_tool
            function_result = _clarify_tool(
                question=function_args.get("question", ""),
                choices=function_args.get("choices"),
                callback=agent.clarify_callback,
            )
            tool_duration = time.time() - tool_start_time
            if agent._should_emit_quiet_tool_messages():
                agent._vprint(f"  {_get_cute_tool_message_impl('clarify', function_args, tool_duration, result=function_result)}")
        elif function_name == "read_terminal":
            from tools.read_terminal_tool import read_terminal_tool as _read_terminal_tool
            function_result = _read_terminal_tool(
                start_line=function_args.get("start_line"),
                count=function_args.get("count"),
                callback=getattr(agent, "read_terminal_callback", None),
            )
            tool_duration = time.time() - tool_start_time
            if agent._should_emit_quiet_tool_messages():
                agent._vprint(f"  {_get_cute_tool_message_impl('read_terminal', function_args, tool_duration, result=function_result)}")
        elif function_name == "sync_canonical_checkout":
            from tools.canonical_checkout_sync_tool import sync_canonical_checkout_tool

            function_result = sync_canonical_checkout_tool(
                project_path=function_args.get("project_path", ""),
                branch=function_args.get("branch", ""),
                merge_commit=function_args.get("merge_commit", ""),
                parent_agent=agent,
            )
            tool_duration = time.time() - tool_start_time
            if agent._should_emit_quiet_tool_messages():
                agent._vprint(
                    f"  {_get_cute_tool_message_impl('sync_canonical_checkout', function_args, tool_duration, result=function_result)}"
                )
        elif function_name == "delegate_task":
            tasks_arg = function_args.get("tasks")
            if tasks_arg and isinstance(tasks_arg, list):
                spinner_label = f"🔀 delegating {len(tasks_arg)} tasks · (/agents to monitor)"
            else:
                goal_preview = (function_args.get("goal") or "")[:30]
                spinner_label = (
                    f"🔀 {goal_preview} · (/agents to monitor)"
                    if goal_preview
                    else "🔀 delegating · (/agents to monitor)"
                )
            spinner = None
            if agent._should_emit_quiet_tool_messages() and agent._should_start_quiet_spinner():
                face = random.choice(KawaiiSpinner.get_waiting_faces())
                spinner = KawaiiSpinner(f"{face} {spinner_label}", spinner_type='dots', print_fn=agent._print_fn)
                spinner.start()
            agent._delegate_spinner = spinner
            _delegate_result = None
            try:
                function_result = agent._dispatch_delegate_task(function_args)
                _delegate_result = function_result
            finally:
                agent._delegate_spinner = None
                tool_duration = time.time() - tool_start_time
                cute_msg = _get_cute_tool_message_impl('delegate_task', function_args, tool_duration, result=_delegate_result)
                if spinner:
                    spinner.stop(cute_msg)
                elif agent._should_emit_quiet_tool_messages():
                    agent._vprint(f"  {cute_msg}")
        elif function_name == "delegate_coding_task":
            task_preview = (function_args.get("task") or "")[:30]
            spinner_label = f"worker {task_preview}" if task_preview else "coding worker"
            spinner = None
            if agent._should_emit_quiet_tool_messages() and agent._should_start_quiet_spinner():
                face = random.choice(KawaiiSpinner.get_waiting_faces())
                spinner = KawaiiSpinner(
                    f"{face} {spinner_label}",
                    spinner_type="dots",
                    print_fn=agent._print_fn,
                )
                spinner.start()
            _coding_worker_result = None
            try:
                function_args = dict(function_args or {})
                function_args["_parent_messages"] = list(messages or [])
                function_result = agent._dispatch_coding_task(function_args)
                _coding_worker_result = function_result
                if _coding_worker_result_attempted(function_result):
                    agent._coding_worker_used_this_turn = True
            finally:
                tool_duration = time.time() - tool_start_time
                cute_msg = _get_cute_tool_message_impl(
                    "delegate_coding_task",
                    function_args,
                    tool_duration,
                    result=_coding_worker_result,
                )
                if spinner:
                    spinner.stop(cute_msg)
                elif agent._should_emit_quiet_tool_messages():
                    agent._vprint(f"  {cute_msg}")
        elif agent._context_engine_tool_names and function_name in agent._context_engine_tool_names:
            # Context engine tools (lcm_grep, lcm_describe, lcm_expand, etc.)
            spinner = None
            if agent._should_emit_quiet_tool_messages():
                face = random.choice(KawaiiSpinner.get_waiting_faces())
                emoji = _get_tool_emoji(function_name)
                preview = _build_tool_preview(function_name, function_args) or function_name
                spinner = KawaiiSpinner(f"{face} {emoji} {preview}", spinner_type='dots', print_fn=agent._print_fn)
                spinner.start()
            _ce_result = None
            try:
                function_result = agent.context_compressor.handle_tool_call(function_name, function_args, messages=messages)
                _ce_result = function_result
            except Exception as tool_error:
                function_result = json.dumps({"error": f"Context engine tool '{function_name}' failed: {tool_error}"})
                logger.error("context_engine.handle_tool_call raised for %s: %s", function_name, tool_error, exc_info=True)
            finally:
                tool_duration = time.time() - tool_start_time
                cute_msg = _get_cute_tool_message_impl(function_name, function_args, tool_duration, result=_ce_result)
                if spinner:
                    spinner.stop(cute_msg)
                elif agent._should_emit_quiet_tool_messages():
                    agent._vprint(f"  {cute_msg}")
        elif agent._memory_manager and agent._memory_manager.has_tool(function_name):
            # Memory provider tools (hindsight_retain, honcho_search, etc.)
            # These are not in the tool registry — route through MemoryManager.
            spinner = None
            if agent._should_emit_quiet_tool_messages() and agent._should_start_quiet_spinner():
                face = random.choice(KawaiiSpinner.get_waiting_faces())
                emoji = _get_tool_emoji(function_name)
                preview = _build_tool_preview(function_name, function_args) or function_name
                spinner = KawaiiSpinner(f"{face} {emoji} {preview}", spinner_type='dots', print_fn=agent._print_fn)
                spinner.start()
            _mem_result = None
            try:
                function_result = agent._memory_manager.handle_tool_call(function_name, function_args)
                _mem_result = function_result
            except Exception as tool_error:
                function_result = json.dumps({"error": f"Memory tool '{function_name}' failed: {tool_error}"})
                logger.error("memory_manager.handle_tool_call raised for %s: %s", function_name, tool_error, exc_info=True)
            finally:
                tool_duration = time.time() - tool_start_time
                cute_msg = _get_cute_tool_message_impl(function_name, function_args, tool_duration, result=_mem_result)
                if spinner:
                    spinner.stop(cute_msg)
                elif agent._should_emit_quiet_tool_messages():
                    agent._vprint(f"  {cute_msg}")
        elif agent.quiet_mode:
            spinner = None
            if agent._should_emit_quiet_tool_messages() and agent._should_start_quiet_spinner():
                face = random.choice(KawaiiSpinner.get_waiting_faces())
                emoji = _get_tool_emoji(function_name)
                preview = _build_tool_preview(function_name, function_args) or function_name
                spinner = KawaiiSpinner(f"{face} {emoji} {preview}", spinner_type='dots', print_fn=agent._print_fn)
                spinner.start()
            _spinner_result = None
            try:
                function_result = _ra().handle_function_call(
                    function_name, function_args, effective_task_id,
                    tool_call_id=tool_call.id,
                    session_id=agent.session_id or "",
                    enabled_tools=list(agent.valid_tool_names) if agent.valid_tool_names else None,
                    skip_pre_tool_call_hook=True,
                    skip_tool_request_middleware=True,
                    enabled_toolsets=getattr(agent, "enabled_toolsets", None),
                    disabled_toolsets=getattr(agent, "disabled_toolsets", None),
                    runtime_mode=getattr(agent, "_runtime_mode", None),
                )
                _spinner_result = function_result
            except Exception as tool_error:
                function_result = f"Error executing tool '{function_name}': {tool_error}"
                logger.error("handle_function_call raised for %s: %s", function_name, tool_error, exc_info=True)
            else:
                _hooks_applied_by_dispatch = True
            finally:
                tool_duration = time.time() - tool_start_time
                cute_msg = _get_cute_tool_message_impl(function_name, function_args, tool_duration, result=_spinner_result)
                if spinner:
                    spinner.stop(cute_msg)
                elif agent._should_emit_quiet_tool_messages():
                    agent._vprint(f"  {cute_msg}")
        else:
            try:
                function_result = _ra().handle_function_call(
                    function_name, function_args, effective_task_id,
                    tool_call_id=tool_call.id,
                    session_id=agent.session_id or "",
                    enabled_tools=list(agent.valid_tool_names) if agent.valid_tool_names else None,
                    skip_pre_tool_call_hook=True,
                    skip_tool_request_middleware=True,
                    enabled_toolsets=getattr(agent, "enabled_toolsets", None),
                    disabled_toolsets=getattr(agent, "disabled_toolsets", None),
                    runtime_mode=getattr(agent, "_runtime_mode", None),
                )
            except Exception as tool_error:
                function_result = f"Error executing tool '{function_name}': {tool_error}"
                logger.error("handle_function_call raised for %s: %s", function_name, tool_error, exc_info=True)
            else:
                _hooks_applied_by_dispatch = True
            tool_duration = time.time() - tool_start_time

        if not _execution_blocked and not _hooks_applied_by_dispatch:
            function_result = apply_tool_result_hooks(
                function_name,
                function_args,
                function_result,
                task_id=effective_task_id or "",
                session_id=agent.session_id or "",
                tool_call_id=tool_call.id,
                duration_ms=int(tool_duration * 1000),
                middleware_trace=list(middleware_trace),
            )

        _closeout_accepted_now = False
        _verification_result = function_result
        if not _execution_blocked:
            _full_closeout_result = function_result
            function_result, _closeout_accepted_now = _process_closeout_receipt(
                agent,
                function_name,
                function_args,
                function_result,
            )
            function_result = _attach_closeout_log_reference(
                function_result,
                _full_closeout_result,
                tool_call_id=str(getattr(tool_call, "id", "") or "closeout"),
                effective_task_id=effective_task_id,
            )
            closeout_boundary_hit = closeout_boundary_hit or _closeout_accepted_now

        if not _execution_blocked:
            function_result = _apply_preview_readiness_result(
                agent,
                function_name,
                function_args,
                function_result,
            )

        if isinstance(function_result, str):
            result_preview = function_result if agent.verbose_logging else (
                function_result[:200] if len(function_result) > 200 else function_result
            )
            _result_len = len(function_result)
        else:
            # Multimodal dict result (_multimodal=True) — not sliceable as string
            result_preview = function_result
            _result_len = len(str(function_result))

        # Log tool errors to the persistent error log so [error] tags
        # in the UI always have a corresponding detailed entry on disk.
        _is_error_result, _ = _detect_tool_failure(function_name, function_result)
        if not _execution_blocked:
            function_result = agent._append_guardrail_observation(
                function_name,
                function_args,
                function_result,
                failed=_is_error_result,
            )
            result_preview = function_result if agent.verbose_logging else (
                function_result[:200] if len(function_result) > 200 else function_result
            )
        if _is_error_result:
            logger.warning("Tool %s returned error (%.2fs): %s", function_name, tool_duration, result_preview)
        else:
            logger.info("tool %s completed (%.2fs, %d chars)", function_name, tool_duration, _result_len)
        finish_agent_runtime_span(
            agent,
            runtime_span,
            status=(
                "blocked"
                if _execution_blocked
                else "error"
                if _is_error_result
                else "ok"
            ),
        )
        _record_turn_tool_runtime(
            agent,
            function_name,
            tool_duration,
            function_result,
            _is_error_result,
            blocked=_execution_blocked,
        )
        if not _execution_blocked:
            _record_turn_verification_evidence(
                agent,
                function_name,
                storage_args,
                (
                    _verification_result
                    if function_name == "terminal"
                    else function_result
                ),
                _is_error_result,
                tool_duration,
                visual_assertion_args=function_args,
            )
            function_result, _closeout_promoted_now = _promote_pending_closeout_receipt(
                agent,
                function_name,
                function_result,
            )
            if _closeout_promoted_now:
                _closeout_accepted_now = True
                closeout_boundary_hit = True

        # Track file-mutation outcome for the turn-end verifier.  See
        # the concurrent path for the rationale; both paths must feed
        # the same state so the footer reflects every tool call in the
        # turn, not just the parallel ones.
        if not _execution_blocked:
            try:
                agent._record_file_mutation_result(
                    function_name, storage_args, function_result, _is_error_result,
                )
                _record_coding_worker_mutation_paths(
                    agent,
                    function_name,
                    function_result,
                )
                _record_visual_qa_edit_order(
                    agent,
                    function_name,
                    function_result,
                    task_id=effective_task_id,
                    tool_runtime_recorded=True,
                )
            except Exception as _ver_err:
                logging.debug("file-mutation verifier record failed: %s", _ver_err)

        if not _execution_blocked and agent.tool_progress_callback:
            try:
                agent.tool_progress_callback(
                    "tool.completed", function_name, None, None,
                    duration=tool_duration, is_error=_is_error_result,
                    result=function_result,
                )
            except Exception as cb_err:
                logging.debug(f"Tool progress callback error: {cb_err}")

        agent._current_tool = None
        agent._touch_activity(f"tool completed: {function_name} ({tool_duration:.1f}s)")
        if not _execution_blocked and not _is_error_result:
            try:
                agent._provider_no_progress_mark_progress(
                    "successful_tool_call", phase="tool_execution"
                )
            except Exception:
                pass

        if agent.verbose_logging:
            logging.debug(f"Tool {function_name} completed in {tool_duration:.2f}s")
            _log_result = _multimodal_text_summary(function_result)
            logging.debug(f"Tool result ({len(_log_result)} chars): {_log_result}")

        if not _execution_blocked and agent.tool_complete_callback:
            try:
                agent.tool_complete_callback(tool_call.id, function_name, storage_args, function_result)
            except Exception as cb_err:
                logging.debug(f"Tool complete callback error: {cb_err}")

        function_result = maybe_persist_tool_result(
            content=function_result,
            tool_name=function_name,
            tool_use_id=tool_call.id,
            env=get_active_env(effective_task_id),
        ) if not _is_multimodal_tool_result(function_result) else function_result

        # Discover subdirectory context files from tool arguments
        subdir_hints = agent._subdirectory_hints.check_tool_call(function_name, storage_args)
        if subdir_hints:
            if _is_multimodal_tool_result(function_result):
                _append_subdir_hint_to_multimodal(function_result, subdir_hints)
            else:
                function_result += subdir_hints

        # Unwrap _multimodal dicts to an OpenAI-style content list
        # (see parallel path for rationale). String results pass through.
        _tool_content = agent._tool_result_content_for_active_model(function_name, function_result)

        tool_message = make_tool_result_message(function_name, _tool_content, tool_call.id)
        messages.append(tool_message)
        risk_metadata = tool_message.get("_tool_output_risk")
        if (
            risk_metadata is not None
            and risk_metadata.get("risk") != "low"
            and agent.tool_progress_callback
        ):
            try:
                agent.tool_progress_callback(
                    "tool.output_risk",
                    function_name,
                    None,
                    None,
                    tool_call_id=tool_call.id,
                    risk_metadata=risk_metadata,
                )
            except Exception as cb_err:
                logging.debug("Tool output risk callback error: %s", cb_err)
        _flush_session_db_after_tool_progress(
            agent,
            messages,
            stage=f"tool result {function_name}",
        )

        if _closeout_accepted_now and i < len(assistant_message.tool_calls):
            _append_closeout_skipped_tool_results(
                agent,
                messages,
                assistant_message.tool_calls[i:],
            )
            steer_boundary_hit = True
            break

        if not agent.quiet_mode:
            if agent.verbose_logging:
                print(f"  ✅ Tool {i} completed in {tool_duration:.2f}s")
                print(agent._wrap_verbose("Result: ", function_result))
            else:
                _fr_str = function_result if isinstance(function_result, str) else str(function_result)
                response_preview = _fr_str[:agent.log_prefix_chars] + "..." if len(_fr_str) > agent.log_prefix_chars else _fr_str
                print(f"  ✅ Tool {i} completed in {tool_duration:.2f}s - {response_preview}")

        if agent._interrupt_requested and i < len(assistant_message.tool_calls):
            remaining = len(assistant_message.tool_calls) - i
            agent._vprint(f"{agent.log_prefix}⚡ Interrupt: skipping {remaining} remaining tool call(s)", force=True)
            for skipped_tc in assistant_message.tool_calls[i:]:
                skipped_name = skipped_tc.function.name
                messages.append(make_tool_result_message(
                    skipped_name,
                    f"[Tool execution skipped — {skipped_name} was not started. User sent a new message]",
                    skipped_tc.id,
                    effect_disposition="none",
                ))
            break

        if _agent_has_pending_steer(agent):
            remaining_calls = assistant_message.tool_calls[i:]
            if remaining_calls:
                append_steer_skipped_tool_results(
                    agent,
                    messages,
                    remaining_calls,
                )
                steer_boundary_hit = True
                break
            if not finalize:
                # The containing segmented dispatcher owns later calls and
                # will attach the still-pending guidance to their final skip.
                steer_boundary_hit = True
                break

        if agent.tool_delay > 0 and i < len(assistant_message.tool_calls):
            time.sleep(agent.tool_delay)

    # ── Per-turn aggregate budget enforcement ─────────────────────────
    num_tools_seq = len(assistant_message.tool_calls)

    if finalize and num_tools_seq > 0:
        enforce_turn_budget(messages[-num_tools_seq:], env=get_active_env(effective_task_id), config=_tool_budget)

    # ── /steer injection ──────────────────────────────────────────────
    # See _execute_tool_calls_parallel for the rationale. Same hook,
    # applied to sequential execution as well.
    if finalize and num_tools_seq > 0:
        agent._apply_pending_steer_to_tool_results(messages, num_tools_seq)
        rewrite_messages = getattr(agent, "_rewrite_messages_to_session_db", None)
        if callable(rewrite_messages):
            rewrite_messages(messages)
    return steer_boundary_hit or closeout_boundary_hit




def execute_tool_calls_segmented(agent, assistant_message, messages: list, effective_task_id: str, api_call_count: int = 0, segments=None) -> None:
    """Execute a mixed tool-call batch as ordered parallel/sequential segments.

    ``segments`` is the ``(kind, calls)`` plan from
    ``_plan_tool_batch_segments``: maximal contiguous runs of parallel-safe
    calls execute on the concurrent path, barrier calls on the sequential
    path, strictly in the model's original call order. Because segments are
    contiguous, every tool result is still appended one-per-call in emission
    order and no call ever starts before an earlier barrier finishes —
    identical ordering and side-effect boundaries to fully-sequential
    execution, with I/O parallelism recovered inside the safe runs.

    Turn-end work (aggregate budget enforcement + /steer injection) is done
    once here for the WHOLE batch; the per-segment executor calls run with
    ``finalize=False`` so a multi-segment turn cannot multiply the budget or
    truncate a steer marker.

    Interrupt semantics: each segment executor already checks
    ``agent._interrupt_requested`` up front and appends a cancelled/skipped
    result per call, so an interrupt during segment *k* drains segments
    *k+1..n* without executing them while preserving one result per
    tool_call_id.
    """
    from types import SimpleNamespace

    if segments is None:
        _active_env = get_active_env(effective_task_id)
        _exec_cwd = Path(_active_env.cwd) if _active_env is not None and _active_env.cwd else None
        segments = _plan_tool_batch_segments(assistant_message.tool_calls, execution_cwd=_exec_cwd)

    stop_after_segment = False
    for segment_index, (kind, calls) in enumerate(segments):
        if stop_after_segment or _agent_has_pending_steer(agent):
            later_calls = [
                tc
                for _later_kind, later_segment in segments[segment_index:]
                for tc in later_segment
            ]
            if getattr(agent, "_accepted_closeout_receipt", None):
                _append_closeout_skipped_tool_results(agent, messages, later_calls)
            else:
                append_steer_skipped_tool_results(
                    agent,
                    messages,
                    later_calls,
                    stage="segmented steer-skipped tool result",
                )
            stop_after_segment = True
            break
        segment_message = SimpleNamespace(tool_calls=list(calls))
        if kind == "parallel":
            execute_tool_calls_concurrent(
                agent, segment_message, messages, effective_task_id, api_call_count,
                finalize=False,
            )
        else:
            stop_after_segment = bool(execute_tool_calls_sequential(
                agent, segment_message, messages, effective_task_id, api_call_count,
                finalize=False,
            ))

        if _agent_has_pending_steer(agent):
            later_calls = [
                tc
                for _later_kind, later_segment in segments[segment_index + 1:]
                for tc in later_segment
            ]
            if later_calls:
                append_steer_skipped_tool_results(
                    agent,
                    messages,
                    later_calls,
                    stage="segmented steer-skipped tool result",
                )
            stop_after_segment = True
            break

    # ── Whole-turn finalize (budget + /steer) ─────────────────────────
    total_tools = len(assistant_message.tool_calls)
    if total_tools > 0:
        _tool_budget = _budget_for_agent(agent)
        enforce_turn_budget(
            messages[-total_tools:],
            env=get_active_env(effective_task_id),
            config=_tool_budget,
        )
        agent._apply_pending_steer_to_tool_results(messages, total_tools)
        rewrite_messages = getattr(agent, "_rewrite_messages_to_session_db", None)
        if callable(rewrite_messages):
            rewrite_messages(messages)


__all__ = [
    "execute_tool_calls_concurrent",
    "execute_tool_calls_sequential",
    "execute_tool_calls_segmented",
    "append_steer_skipped_tool_results",
]
