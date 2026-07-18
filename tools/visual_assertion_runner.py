"""Bounded execution of declarative visual assertions on persistent CDP."""

from __future__ import annotations

import asyncio
import base64
import inspect
import threading
import time
from typing import Any, Awaitable, Callable, Optional

from agent.execution_guard import CooperativeExecutionGuard, ExecutionGuardExpired
from agent.visual_assertions import (
    aggregate_assertion_results,
    validate_visual_assertions,
    visual_assertion_contract_id,
)
from agent.visual_qa import (
    normalize_visual_qa_config,
    normalize_visual_requirement,
    visual_requirement_id,
)


_MUTATION_LOCK = threading.Lock()
_MUTATION_GENERATIONS: dict[str, int] = {}


def record_trusted_visual_mutation(task_id: str) -> int:
    """Advance the host-owned mutation generation for a task."""

    key = str(task_id or "default")
    with _MUTATION_LOCK:
        generation = _MUTATION_GENERATIONS.get(key, 0) + 1
        _MUTATION_GENERATIONS[key] = generation
        return generation


def trusted_visual_mutation_token(task_id: str) -> int:
    key = str(task_id or "default")
    with _MUTATION_LOCK:
        return _MUTATION_GENERATIONS.get(key, 0)


def clear_trusted_visual_mutation(task_id: str) -> None:
    """Discard the task-scoped mutation generation during resource cleanup."""

    key = str(task_id or "default")
    with _MUTATION_LOCK:
        _MUTATION_GENERATIONS.pop(key, None)


def _accepts_keyword(function: Callable[..., Any], keyword: str) -> bool:
    try:
        parameters = inspect.signature(function).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD or parameter.name == keyword
        for parameter in parameters
    )


def _declares_keyword(function: Callable[..., Any], keyword: str) -> bool:
    try:
        return keyword in inspect.signature(function).parameters
    except (TypeError, ValueError):
        return False


async def _thread_call(
    function: Callable[..., Any],
    *args: Any,
    execution_guard: Optional[CooperativeExecutionGuard] = None,
    **kwargs: Any,
) -> Any:
    def _invoke() -> Any:
        if execution_guard is not None:
            execution_guard.check()
            if _accepts_keyword(function, "execution_guard"):
                kwargs["execution_guard"] = execution_guard
        return function(*args, **kwargs)

    return await asyncio.to_thread(_invoke)


async def _deterministic_result(
    supervisor: Any,
    assertion: dict[str, Any],
    *,
    execution_guard: CooperativeExecutionGuard,
) -> dict[str, Any]:
    assertion_id = assertion["id"]
    kind = assertion["kind"]
    if kind in {
        "exists",
        "not_exists",
        "visible",
        "viewport_contained",
        "no_horizontal_overflow",
        "count",
    }:
        state = await _thread_call(
            supervisor.trusted_element_state,
            assertion["locator"],
            execution_guard=execution_guard,
        )
        if not state.get("ok"):
            return {"id": assertion_id, "status": "blocked", "code": "element_lookup_unavailable"}
        if kind == "exists":
            passed = state.get("exists") is True
        elif kind == "not_exists":
            passed = state.get("exists") is False
        elif kind == "visible":
            passed = state.get("visible") is True
        elif kind == "viewport_contained":
            passed = state.get("viewport_contained") is True
        elif kind == "no_horizontal_overflow":
            passed = state.get("no_horizontal_overflow") is True
        else:
            count = int(state.get("count") or 0)
            passed = assertion["min"] <= count <= assertion["max"]
        return {
            "id": assertion_id,
            "status": "passed" if passed else "failed",
            "code": f"{kind}_{'satisfied' if passed else 'mismatch'}",
        }
    if kind == "text_present":
        state = await _thread_call(
            supervisor.trusted_text_present,
            assertion["text"],
            execution_guard=execution_guard,
        )
        if not state.get("ok"):
            return {"id": assertion_id, "status": "blocked", "code": "text_check_unavailable"}
        passed = state.get("present") is True
        return {
            "id": assertion_id,
            "status": "passed" if passed else "failed",
            "code": "text_present" if passed else "text_missing",
        }
    if kind == "no_new_diagnostics":
        state = await _thread_call(
            supervisor.diagnostics_since,
            assertion["cursor"],
            execution_guard=execution_guard,
        )
        if not state.get("ok", True):
            codes = state.get("codes") if isinstance(state.get("codes"), list) else []
            code = codes[0] if codes and codes[0] in {
                "invalid_diagnostic_cursor",
                "diagnostic_history_evicted",
            } else "invalid_diagnostic_cursor"
            return {
                "id": assertion_id,
                "status": "blocked",
                "code": code,
            }
        passed = not state.get("codes")
        return {
            "id": assertion_id,
            "status": "passed" if passed else "failed",
            "code": "no_new_diagnostics" if passed else "new_page_diagnostics",
        }
    return {"id": assertion_id, "status": "uncertain", "code": "unsupported_assertion"}


async def _run_attempt(
    supervisor: Any,
    assertions: list[dict[str, Any]],
    *,
    vision_evaluator: Callable[..., Awaitable[dict[str, Any]]],
    provider: str,
    model: str,
    base_url: str,
    api_key: str,
    api_mode: str,
    cfg: Optional[dict[str, Any]],
    vision_timeout_s: float,
    vision_allowed: bool,
    execution_guard: CooperativeExecutionGuard,
    on_provider_start: Callable[[], None],
) -> dict[str, Any]:
    deterministic = [item for item in assertions if item["kind"] != "screenshot_appearance"]
    appearance = [item for item in assertions if item["kind"] == "screenshot_appearance"]
    results = [
        await _deterministic_result(
            supervisor,
            item,
            execution_guard=execution_guard,
        )
        for item in deterministic
    ]
    provider_started = False

    def _mark_provider_started() -> None:
        nonlocal provider_started
        execution_guard.check()
        if not provider_started:
            on_provider_start()
            provider_started = True

    if appearance:
        if not vision_allowed:
            results.extend(
                {"id": item["id"], "status": "uncertain", "code": "vision_budget_exhausted"}
                for item in appearance
            )
        else:
            screenshot = await _thread_call(
                supervisor.capture_screenshot_memory,
                execution_guard=execution_guard,
            )
            if not screenshot.get("ok"):
                results.extend(
                    {"id": item["id"], "status": "blocked", "code": "screenshot_unavailable"}
                    for item in appearance
                )
            else:
                raw = screenshot.pop("image_bytes", b"")
                data_url = "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
                raw = b""
                evaluator_kwargs = {
                    "provider": provider,
                    "model": model,
                    "base_url": base_url,
                    "api_key": api_key,
                    "api_mode": api_mode,
                    "cfg": cfg,
                    "timeout_s": vision_timeout_s,
                }
                if _declares_keyword(vision_evaluator, "on_provider_start"):
                    evaluator_kwargs["on_provider_start"] = _mark_provider_started
                else:
                    # A custom evaluator is itself the only observable provider
                    # boundary when it cannot expose a finer-grained callback.
                    _mark_provider_started()
                execution_guard.check()
                vision_result = await vision_evaluator(
                    data_url,
                    appearance,
                    **evaluator_kwargs,
                )
                data_url = ""
                results.extend(vision_result.get("results") or [])
    aggregate = aggregate_assertion_results(results)
    aggregate["vision_calls"] = int(provider_started)
    return aggregate


async def run_visual_assertions(
    *,
    task_id: str,
    requirement: Any,
    assertions: Any,
    config: Any = None,
    provider: str = "",
    model: str = "",
    base_url: str = "",
    api_key: str = "",
    api_mode: str = "",
    supervisor: Any = None,
    vision_evaluator: Optional[Callable[..., Awaitable[dict[str, Any]]]] = None,
) -> dict[str, Any]:
    """Run one attempt plus one state-change-gated retry under hard deadlines."""

    visual_config = normalize_visual_qa_config(config)
    total_started = time.monotonic()
    deadline = total_started + visual_config["total_timeout_s"]
    execution_guard = CooperativeExecutionGuard(deadline)

    def _remaining() -> float:
        return execution_guard.remaining()

    normalized_requirement = normalize_visual_requirement(requirement)
    normalized_assertions = validate_visual_assertions(
        assertions,
        max_assertions=visual_config["max_assertions"],
    )
    if normalized_requirement["level"] == "none" or not normalized_assertions:
        return {"status": "uncertain", "code": "invalid_visual_contract", "attempts": []}
    requirement_id = visual_requirement_id(normalized_requirement)
    contract_id = visual_assertion_contract_id(normalized_assertions)
    assertion_ids = [item["id"] for item in normalized_assertions]

    def _receipt(
        status: str,
        *,
        attempts_count: int = 0,
        vision_count: int = 0,
        results: Any = None,
    ) -> dict[str, Any]:
        codes = []
        for item in results if isinstance(results, list) else []:
            code = str(item.get("code") or "") if isinstance(item, dict) else ""
            if code and code not in codes:
                codes.append(code)
        return {
            "requirement_id": requirement_id,
            "contract_id": contract_id,
            "assertion_ids": assertion_ids,
            "status": status,
            "attempts": max(0, min(int(attempts_count), 2)),
            "vision_calls": max(0, min(int(vision_count), 1)),
            "duration_ms": max(
                0,
                min(int((time.monotonic() - total_started) * 1000), 60_000),
            ),
            "diagnostic_codes": codes[:12],
        }

    if supervisor is None:
        try:
            from tools.browser_tool import _ensure_cdp_supervisor, _last_session_key
            from tools.browser_supervisor import SUPERVISOR_REGISTRY

            remaining = _remaining()
            if remaining <= 0:
                raise TimeoutError("visual QA total deadline exhausted")
            effective_task_id = _last_session_key(task_id or "default")
            await asyncio.wait_for(
                _thread_call(
                    _ensure_cdp_supervisor,
                    effective_task_id,
                    execution_guard=execution_guard,
                ),
                timeout=remaining,
            )
            execution_guard.check()
            supervisor = SUPERVISOR_REGISTRY.get(effective_task_id)
        except TimeoutError:
            execution_guard.cancel()
            supervisor = None
        except asyncio.CancelledError:
            execution_guard.cancel()
            raise
        except Exception:
            supervisor = None
    try:
        remaining = _remaining()
        if supervisor is None or remaining <= 0:
            active = False
        else:
            snapshot = await asyncio.wait_for(
                _thread_call(
                    supervisor.snapshot,
                    execution_guard=execution_guard,
                ),
                timeout=remaining,
            )
            active = bool(snapshot.active)
    except TimeoutError:
        execution_guard.cancel()
        active = False
    except asyncio.CancelledError:
        execution_guard.cancel()
        raise
    except Exception:
        active = False
    if supervisor is None or not active:
        receipt = _receipt("blocked")
        return {
            "status": "blocked",
            "code": "browser_supervisor_unavailable",
            "attempts": [],
            "visual_qa_receipt": receipt,
        }
    if vision_evaluator is None:
        from agent.vision_assertions import evaluate_screenshot_assertions

        vision_evaluator = evaluate_screenshot_assertions

    locators = [item["locator"] for item in normalized_assertions if "locator" in item]
    attempts: list[dict[str, Any]] = []
    vision_calls = 0
    final: dict[str, Any] = {"status": "uncertain", "results": []}
    for attempt_index in range(visual_config["max_attempts"]):
        remaining = _remaining()
        if remaining <= 0:
            execution_guard.cancel()
            final = {"status": "uncertain", "results": [], "code": "total_timeout"}
            break
        mutation_before = trusted_visual_mutation_token(task_id)
        try:
            fingerprint_before = await asyncio.wait_for(
                _thread_call(
                    supervisor.trusted_state_fingerprint,
                    locators,
                    execution_guard=execution_guard,
                ),
                timeout=remaining,
            )
        except TimeoutError:
            execution_guard.cancel()
            final = {"status": "uncertain", "results": [], "code": "total_timeout"}
            break
        except asyncio.CancelledError:
            execution_guard.cancel()
            raise
        started = time.monotonic()
        attempt_timeout = min(visual_config["attempt_timeout_s"], _remaining())
        attempt_guard = CooperativeExecutionGuard(
            min(deadline, time.monotonic() + attempt_timeout)
        )
        vision_allowed = vision_calls < visual_config["max_vision_calls"]
        attempt_vision_calls = 0

        def _provider_started() -> None:
            nonlocal vision_calls, attempt_vision_calls
            execution_guard.check()
            attempt_guard.check()
            if vision_calls >= visual_config["max_vision_calls"]:
                raise ExecutionGuardExpired("vision provider budget exhausted")
            vision_calls += 1
            attempt_vision_calls += 1

        try:
            final = await asyncio.wait_for(
                _run_attempt(
                    supervisor,
                    normalized_assertions,
                    vision_evaluator=vision_evaluator,
                    provider=provider,
                    model=model,
                    base_url=base_url,
                    api_key=api_key,
                    api_mode=api_mode,
                    cfg=config if isinstance(config, dict) else None,
                    vision_timeout_s=attempt_timeout,
                    vision_allowed=vision_allowed,
                    execution_guard=attempt_guard,
                    on_provider_start=_provider_started,
                ),
                timeout=attempt_timeout,
            )
        except TimeoutError:
            attempt_guard.cancel()
            final = {
                "status": "uncertain",
                "results": [
                    {"id": item["id"], "status": "uncertain", "code": "attempt_timeout"}
                    for item in normalized_assertions
                ],
                "vision_calls": attempt_vision_calls,
            }
        except asyncio.CancelledError:
            attempt_guard.cancel()
            execution_guard.cancel()
            raise
        duration = min(time.monotonic() - started, visual_config["attempt_timeout_s"])
        attempts.append(
            {
                "attempt": attempt_index + 1,
                "status": final.get("status") or "uncertain",
                "duration_s": round(max(0.0, duration), 3),
                "vision_calls": attempt_vision_calls,
            }
        )
        if final.get("status") == "passed" or attempt_index + 1 >= visual_config["max_attempts"]:
            break
        remaining = _remaining()
        if remaining <= 0:
            execution_guard.cancel()
            final["status"] = "uncertain"
            final["code"] = "total_timeout"
            break
        try:
            fingerprint_after = await asyncio.wait_for(
                _thread_call(
                    supervisor.trusted_state_fingerprint,
                    locators,
                    execution_guard=execution_guard,
                ),
                timeout=remaining,
            )
        except TimeoutError:
            execution_guard.cancel()
            final["status"] = "uncertain"
            final["code"] = "total_timeout"
            break
        except asyncio.CancelledError:
            execution_guard.cancel()
            raise
        mutation_after = trusted_visual_mutation_token(task_id)
        trusted_mutation_changed = mutation_after != mutation_before
        if not trusted_mutation_changed and fingerprint_after == fingerprint_before:
            break

    status = str(final.get("status") or "uncertain")
    receipt = _receipt(
        status,
        attempts_count=len(attempts),
        vision_count=vision_calls,
        results=final.get("results"),
    )
    output = {
        "status": status,
        "code": final.get("code") or "visual_assertions_complete",
        "results": (final.get("results") or [])[:6],
        "attempts": attempts[:2],
        "visual_qa_receipt": receipt,
    }
    if len(str(output)) > visual_config["max_output_chars"]:
        output["results"] = []
        output["code"] = "output_compacted"
        if status == "passed":
            output["status"] = "uncertain"
            output["visual_qa_receipt"]["status"] = "uncertain"
    return output


__all__ = [
    "clear_trusted_visual_mutation",
    "record_trusted_visual_mutation",
    "run_visual_assertions",
    "trusted_visual_mutation_token",
]
