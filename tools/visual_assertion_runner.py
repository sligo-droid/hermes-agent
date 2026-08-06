"""Bounded execution of declarative visual assertions on persistent CDP."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from agent.execution_guard import CooperativeExecutionGuard, ExecutionGuardExpired
from agent.visual_assertions import (
    aggregate_assertion_results,
    diagnose_orchestrated_visual_contract,
    normalize_assertion_result_coverage,
    validate_visual_execution_contract,
    visual_assertion_contract_id,
    visual_execution_contract_id,
    visual_requirement_for_execution_contract,
)
from agent.visual_qa import (
    normalize_visual_qa_config,
    normalize_visual_requirement,
    visual_requirement_id,
    visual_requirement_uses_orchestrator_contract,
)


_MUTATION_LOCK = threading.Lock()
_MUTATION_GENERATIONS: dict[str, int] = {}
_ARTIFACT_CLEANUP_LOCK = threading.Lock()
_LAST_ARTIFACT_CLEANUP = 0.0
_MAX_SCREENSHOT_EVIDENCE_BYTES = 8 * 1024 * 1024


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


def _visual_qa_artifact_paths(
    task_id: str,
    contract_id: str,
    count: int,
) -> list[Path]:
    """Return stable, opaque screenshot paths and prune stale QA artifacts."""

    from hermes_constants import get_hermes_dir

    screenshots_dir = get_hermes_dir("cache/screenshots", "browser_screenshots")
    screenshots_dir.mkdir(parents=True, exist_ok=True)
    global _LAST_ARTIFACT_CLEANUP
    now = time.time()
    with _ARTIFACT_CLEANUP_LOCK:
        if now - _LAST_ARTIFACT_CLEANUP >= 3600:
            _LAST_ARTIFACT_CLEANUP = now
            cutoff = now - 24 * 3600
            for path in screenshots_dir.glob("visual_qa_screenshot_*.png"):
                try:
                    if path.stat().st_mtime < cutoff:
                        path.unlink()
                except OSError:
                    pass
    opaque_run_id = hashlib.sha256(
        f"{task_id or 'default'}:{contract_id}:{secrets.token_hex(12)}".encode("utf-8")
    ).hexdigest()[:24]
    return [
        screenshots_dir / f"visual_qa_screenshot_{opaque_run_id}_{index + 1}.png"
        for index in range(max(0, min(int(count), 4)))
    ]


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


async def _acquire_viewport_scope(
    supervisor: Any,
    viewport: Optional[dict[str, Any]],
    *,
    deadline: float,
    execution_guard: CooperativeExecutionGuard,
) -> dict[str, Any]:
    """Acquire a sync lease without leaving an abandoned thread-owned scope."""

    acquisition_guard = CooperativeExecutionGuard(deadline)
    worker = asyncio.create_task(
        _thread_call(
            supervisor.begin_trusted_viewport_scope,
            viewport,
            execution_guard=acquisition_guard,
        )
    )

    async def _finish_abandoned() -> None:
        acquisition_guard.cancel()
        try:
            result = await asyncio.shield(worker)
        except Exception:
            return
        if isinstance(result, dict) and result.get("ok"):
            try:
                await asyncio.shield(
                    _thread_call(
                        supervisor.end_trusted_viewport_scope,
                        result["token"],
                        result["previous"],
                    )
                )
            except Exception:
                return

    try:
        return await asyncio.wait_for(
            asyncio.shield(worker),
            timeout=max(0.01, deadline - time.monotonic()),
        )
    except TimeoutError:
        await _finish_abandoned()
        return {"ok": False, "code": "viewport_scope_unavailable"}
    except asyncio.CancelledError:
        execution_guard.cancel()
        await _finish_abandoned()
        raise


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
    vision_sweeper: Optional[Callable[..., Awaitable[bool]]],
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
    execution_context: Optional[dict[str, Any]],
    target_locator: Optional[dict[str, str]],
    screenshot_artifacts: list[dict[str, Any]],
    artifact_paths: list[Path],
    artifact_sink: list[dict[str, str]],
    viewport_lease: str = "",
    governing_viewport: Optional[dict[str, Any]] = None,
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
    deterministic_coverage = normalize_assertion_result_coverage(
        results,
        [item["id"] for item in deterministic],
    ) if deterministic else {"valid": True, "results": []}
    results = deterministic_coverage["results"]
    deterministic_blocker = next(
        (item for item in results if item["status"] == "blocked"),
        None,
    )
    if deterministic and not deterministic_coverage["valid"]:
        results.extend(
            {
                "id": item["id"],
                "status": "uncertain",
                "code": "invalid_assertion_results",
            }
            for item in appearance
        )
        aggregate = aggregate_assertion_results(results)
        aggregate["vision_calls"] = 0
        return aggregate
    if deterministic_blocker is not None:
        results.extend(
            {
                "id": item["id"],
                "status": "blocked",
                "code": deterministic_blocker["code"],
            }
            for item in appearance
        )
        aggregate = aggregate_assertion_results(results)
        aggregate["vision_calls"] = 0
        return aggregate
    provider_start_count = 0

    def _mark_provider_started() -> None:
        nonlocal provider_start_count
        execution_guard.check()
        on_provider_start()
        provider_start_count += 1

    if appearance:
        if not vision_allowed:
            results.extend(
                {"id": item["id"], "status": "uncertain", "code": "vision_budget_exhausted"}
                for item in appearance
            )
        else:
            artifact_sink.clear()
            images: list[str] = []
            total_image_bytes = 0
            capture_failed = False
            for index, artifact in enumerate(screenshot_artifacts[:4]):
                screenshot_kwargs: dict[str, Any] = {}
                locator = artifact.get("locator")
                if not isinstance(locator, dict) and len(screenshot_artifacts) == 1:
                    locator = target_locator
                if isinstance(locator, dict) and _declares_keyword(
                    supervisor.capture_screenshot_memory,
                    "locator",
                ):
                    screenshot_kwargs["locator"] = locator
                viewport = artifact.get("viewport")
                if (
                    isinstance(viewport, dict)
                    and {"width", "height"}.issubset(viewport)
                    and _declares_keyword(supervisor.capture_screenshot_memory, "viewport")
                ):
                    screenshot_kwargs["viewport"] = {
                        "width": viewport["width"],
                        "height": viewport["height"],
                    }
                if viewport_lease and _declares_keyword(
                    supervisor.capture_screenshot_memory,
                    "viewport_lease",
                ):
                    screenshot_kwargs["viewport_lease"] = viewport_lease
                screenshot = await _thread_call(
                    supervisor.capture_screenshot_memory,
                    execution_guard=execution_guard,
                    **screenshot_kwargs,
                )
                if viewport_lease and governing_viewport is not None:
                    reapplied = await _thread_call(
                        supervisor.reapply_trusted_viewport_scope,
                        viewport_lease,
                        governing_viewport,
                        execution_guard=execution_guard,
                    )
                    if not reapplied.get("ok"):
                        code = str(reapplied.get("code") or "viewport_reapply_unavailable")
                        lifecycle_results = [
                            {
                                "id": item["id"],
                                "status": "blocked",
                                "code": code,
                            }
                            for item in assertions
                        ]
                        aggregate = aggregate_assertion_results(lifecycle_results)
                        aggregate["vision_calls"] = provider_start_count
                        aggregate["code"] = code
                        return aggregate
                if not screenshot.get("ok"):
                    viewport_code = str(screenshot.get("viewport_code") or "")
                    if viewport_code:
                        lifecycle_results = [
                            {
                                "id": item["id"],
                                "status": "blocked",
                                "code": viewport_code,
                            }
                            for item in assertions
                        ]
                        aggregate = aggregate_assertion_results(lifecycle_results)
                        aggregate["vision_calls"] = provider_start_count
                        aggregate["code"] = viewport_code
                        return aggregate
                    capture_failed = True
                    continue
                raw = screenshot.pop("image_bytes", b"")
                if not isinstance(raw, bytes) or not raw:
                    capture_failed = True
                    continue
                if total_image_bytes + len(raw) > _MAX_SCREENSHOT_EVIDENCE_BYTES:
                    capture_failed = True
                    raw = b""
                    continue
                total_image_bytes += len(raw)
                images.append(
                    "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
                )
                if index < len(artifact_paths):
                    try:
                        await _thread_call(
                            artifact_paths[index].write_bytes,
                            raw,
                            execution_guard=execution_guard,
                        )
                    except Exception:
                        pass
                    else:
                        artifact_sink.append(
                            {
                                "kind": str(artifact.get("kind") or "context"),
                                "screenshot_path": str(artifact_paths[index]),
                            }
                        )
                raw = b""
            if capture_failed or not images:
                results.extend(
                    {"id": item["id"], "status": "blocked", "code": "screenshot_unavailable"}
                    for item in appearance
                )
            else:
                if vision_sweeper is not None:
                    sweep_kwargs = {
                        "cfg": cfg,
                        "timeout_s": vision_timeout_s,
                    }
                    if _declares_keyword(vision_sweeper, "on_provider_start"):
                        sweep_kwargs["on_provider_start"] = _mark_provider_started
                    else:
                        _mark_provider_started()
                    execution_guard.check()
                    if not await vision_sweeper(images, **sweep_kwargs):
                        results.extend(
                            {"id": item["id"], "status": "uncertain", "code": "vision_call_failed"}
                            for item in appearance
                        )
                        aggregate = aggregate_assertion_results(results)
                        aggregate["vision_calls"] = provider_start_count
                        return aggregate
                evaluator_kwargs = {
                    "provider": provider,
                    "model": model,
                    "base_url": base_url,
                    "api_key": api_key,
                    "api_mode": api_mode,
                    "cfg": cfg,
                    "timeout_s": vision_timeout_s,
                }
                if execution_context and _declares_keyword(
                    vision_evaluator,
                    "execution_context",
                ):
                    evaluator_kwargs["execution_context"] = execution_context
                if _declares_keyword(vision_evaluator, "on_provider_start"):
                    evaluator_kwargs["on_provider_start"] = _mark_provider_started
                else:
                    # A custom evaluator is itself the only observable provider
                    # boundary when it cannot expose a finer-grained callback.
                    _mark_provider_started()
                execution_guard.check()
                vision_result = await vision_evaluator(
                    images,
                    appearance,
                    **evaluator_kwargs,
                )
                images = []
                appearance_coverage = normalize_assertion_result_coverage(
                    vision_result.get("results"),
                    [item["id"] for item in appearance],
                    invalid_code="invalid_vision_output",
                )
                results.extend(appearance_coverage["results"])
    combined = normalize_assertion_result_coverage(
        results,
        [item["id"] for item in assertions],
    )
    aggregate = aggregate_assertion_results(combined["results"])
    aggregate["vision_calls"] = provider_start_count
    if "vision_result" in locals() and isinstance(vision_result, dict):
        if vision_result.get("review_model"):
            aggregate["review_model"] = str(vision_result["review_model"])
        if vision_result.get("review_fallback"):
            aggregate["review_fallback"] = str(vision_result["review_fallback"])
    return aggregate


async def run_visual_assertions(
    *,
    task_id: str,
    requirement: Any,
    contract: Any = None,
    assertions: Any = None,
    config: Any = None,
    provider: str = "",
    model: str = "",
    base_url: str = "",
    api_key: str = "",
    api_mode: str = "",
    supervisor: Any = None,
    vision_evaluator: Optional[Callable[..., Awaitable[dict[str, Any]]]] = None,
    vision_sweeper: Optional[Callable[..., Awaitable[bool]]] = None,
) -> dict[str, Any]:
    """Run one attempt plus one state-change-gated retry under hard deadlines."""

    visual_config = normalize_visual_qa_config(config)
    total_started = time.monotonic()
    deadline = total_started + visual_config["total_timeout_s"]
    execution_guard = CooperativeExecutionGuard(deadline)

    def _remaining() -> float:
        return execution_guard.remaining()

    normalized_requirement = normalize_visual_requirement(requirement)
    raw_contract = (
        contract
        if isinstance(contract, dict)
        else {"assertions": assertions}
    )
    standalone_contract = normalized_requirement["level"] == "none"
    orchestrated_contract = (
        standalone_contract
        or visual_requirement_uses_orchestrator_contract(normalized_requirement)
    )
    contract_diagnostic = (
        diagnose_orchestrated_visual_contract(
            raw_contract,
            max_assertions=visual_config["max_assertions"],
        )
        if orchestrated_contract
        else None
    )
    normalized_contract = (
        contract_diagnostic["contract"]
        if contract_diagnostic is not None
        else validate_visual_execution_contract(
            normalized_requirement,
            raw_contract,
            max_assertions=visual_config["max_assertions"],
        )
    )
    normalized_assertions = normalized_contract.get("assertions") or []
    if not normalized_assertions:
        output = {"status": "uncertain", "code": "invalid_visual_contract", "attempts": []}
        if contract_diagnostic is not None:
            output["reason_code"] = contract_diagnostic["reason_code"]
            output["correction"] = contract_diagnostic["correction"]
        return output
    if standalone_contract:
        normalized_requirement = visual_requirement_for_execution_contract(
            normalized_contract,
            max_assertions=visual_config["max_assertions"],
        )
    requirement_id = visual_requirement_id(normalized_requirement)
    contract_id = (
        visual_execution_contract_id(normalized_contract)
        if orchestrated_contract
        else visual_assertion_contract_id(normalized_assertions)
    )
    assertion_ids = [item["id"] for item in normalized_assertions]
    coverage_ids = [
        str(item.get("id") or "")
        for item in normalized_requirement.get("assertions") or []
        if isinstance(item, dict)
    ]
    target = normalized_contract.get("target")
    target_locator = (
        target.get("locator")
        if isinstance(target, dict) and isinstance(target.get("locator"), dict)
        else None
    )
    execution_context = (
        {
            "target": {
                "description": str(target.get("description") or "")
            },
            **{
                key: normalized_contract[key]
                for key in ("page", "viewport", "state")
                if key in normalized_contract
            },
            "artifacts": [
                {
                    key: item[key]
                    for key in ("kind", "description", "viewport")
                    if key in item
                }
                for item in normalized_contract.get("artifacts") or []
                if isinstance(item, dict)
            ],
        }
        if orchestrated_contract and isinstance(target, dict)
        else None
    )

    def _receipt(
        status: str,
        *,
        attempts_count: int = 0,
        vision_count: int = 0,
        results: Any = None,
        lifecycle_codes: Any = None,
    ) -> dict[str, Any]:
        codes = []
        for item in results if isinstance(results, list) else []:
            code = str(item.get("code") or "") if isinstance(item, dict) else ""
            if code and code not in codes:
                codes.append(code)
        for code in lifecycle_codes if isinstance(lifecycle_codes, list) else []:
            code = str(code or "")
            if code and code not in codes:
                codes.append(code)
        receipt = {
            "requirement_id": requirement_id,
            "contract_id": contract_id,
            "assertion_ids": assertion_ids,
            "status": status,
            "attempts": max(0, min(int(attempts_count), 2)),
            "vision_calls": max(0, min(int(vision_count), 2)),
            "duration_ms": max(
                0,
                min(int((time.monotonic() - total_started) * 1000), 60_000),
            ),
            "diagnostic_codes": codes[:12],
        }
        if orchestrated_contract:
            receipt["coverage_ids"] = coverage_ids
        return receipt

    if supervisor is None:
        try:
            from tools.browser_tool import (
                _align_cdp_supervisor_to_current_page,
                _ensure_cdp_supervisor,
                _last_session_key,
            )
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
            if supervisor is not None:
                await asyncio.wait_for(
                    _thread_call(
                        _align_cdp_supervisor_to_current_page,
                        effective_task_id,
                        supervisor=supervisor,
                        execution_guard=execution_guard,
                    ),
                    timeout=max(0.01, _remaining()),
                )
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
            "correction": (
                "Reinitialize the task browser with browser_navigate, restore required "
                "authentication and page state, then retry visual_qa once."
            ),
            "attempts": [],
            "visual_qa_receipt": receipt,
        }
    from agent.vision_assertions import evaluate_screenshot_assertions, run_visual_sweep
    if vision_evaluator is None:
        vision_evaluator = evaluate_screenshot_assertions
    if vision_sweeper is None:
        vision_sweeper = run_visual_sweep

    screenshot_artifacts = normalized_contract.get("artifacts")
    if not isinstance(screenshot_artifacts, list) or not screenshot_artifacts:
        screenshot_artifacts = [
            {
                "kind": "focused" if target_locator else "context",
                "description": "Visual QA evidence",
                **({"locator": target_locator} if target_locator else {}),
                "viewport": {},
            }
        ]
    artifact_paths = _visual_qa_artifact_paths(
        task_id,
        contract_id,
        len(screenshot_artifacts),
    )

    locators: list[dict[str, str]] = []
    for locator in [
        target_locator,
        *(item.get("locator") for item in normalized_assertions),
        *(item.get("locator") for item in screenshot_artifacts),
    ]:
        if isinstance(locator, dict) and locator not in locators:
            locators.append(locator)
    attempts: list[dict[str, Any]] = []
    vision_calls = 0
    final: dict[str, Any] = {"status": "uncertain", "results": []}
    latest_artifacts: list[dict[str, str]] = []
    governing_viewport = normalized_contract.get("viewport")
    if not (
        isinstance(governing_viewport, dict)
        and {"width", "height"}.issubset(governing_viewport)
    ):
        governing_viewport = None
    viewport_scope: Optional[dict[str, Any]] = None
    lifecycle_codes: list[str] = []
    if not all(
        hasattr(supervisor, name)
        for name in (
            "begin_trusted_viewport_scope",
            "end_trusted_viewport_scope",
        )
    ) or (
        governing_viewport is not None
        and not hasattr(supervisor, "reapply_trusted_viewport_scope")
    ):
        viewport_scope = {"ok": False, "code": "viewport_scope_unavailable"}
    else:
        try:
            viewport_scope = await _acquire_viewport_scope(
                supervisor,
                governing_viewport,
                deadline=deadline,
                execution_guard=execution_guard,
            )
        except asyncio.CancelledError:
            raise
    if not viewport_scope.get("ok"):
        code = str(viewport_scope.get("code") or "viewport_scope_unavailable")
        results = [
            {"id": item["id"], "status": "blocked", "code": code}
            for item in normalized_assertions
        ]
        receipt = _receipt("blocked", results=results, lifecycle_codes=[code])
        return {
            "status": "blocked",
            "code": code,
            "results": results,
            "attempts": [],
            "visual_qa_receipt": receipt,
        }

    try:
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
            required_vision_calls = 2 if vision_sweeper is not None else 1
            vision_allowed = (
                vision_calls + required_vision_calls
                <= visual_config["max_vision_calls"]
            )
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
                        vision_sweeper=vision_sweeper,
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
                        execution_context=execution_context,
                        target_locator=target_locator,
                        screenshot_artifacts=screenshot_artifacts,
                        artifact_paths=artifact_paths,
                        artifact_sink=latest_artifacts,
                        viewport_lease=(
                            str(viewport_scope.get("token") or "")
                            if viewport_scope is not None
                            else ""
                        ),
                        governing_viewport=governing_viewport,
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
    finally:
        if viewport_scope is not None and viewport_scope.get("ok"):
            try:
                restored = await asyncio.shield(
                    _thread_call(
                        supervisor.end_trusted_viewport_scope,
                        viewport_scope["token"],
                        viewport_scope["previous"],
                    )
                )
            except Exception:
                restored = {"ok": False, "code": "viewport_restore_unavailable"}
            if not restored.get("ok"):
                code = str(restored.get("code") or "viewport_restore_unavailable")
                lifecycle_codes.append(code)
                if final.get("status") == "passed":
                    final["status"] = "uncertain"
                final["code"] = code

    status = str(final.get("status") or "uncertain")
    receipt = _receipt(
        status,
        attempts_count=len(attempts),
        vision_count=vision_calls,
        results=final.get("results"),
        lifecycle_codes=lifecycle_codes,
    )
    output = {
        "status": status,
        "code": final.get("code") or "visual_assertions_complete",
        "results": (final.get("results") or [])[:6],
        "attempts": attempts[:2],
        "visual_qa_receipt": receipt,
    }
    if final.get("review_model"):
        output["review_model"] = str(final["review_model"])
    if final.get("review_fallback"):
        output["review_fallback"] = str(final["review_fallback"])
    if latest_artifacts:
        output["screenshot_artifacts"] = latest_artifacts[:4]
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
