import asyncio
import time
from types import SimpleNamespace

import pytest

from agent.visual_qa import normalize_visual_requirement
from tools.visual_assertion_runner import (
    record_trusted_visual_mutation,
    run_visual_assertions,
    trusted_visual_mutation_token,
)


_REQUIREMENT = normalize_visual_requirement(
    {
        "level": "surface",
        "target": "mobile toolbar",
        "assertions": ["toolbar remains inside the mobile viewport"],
    }
)
_ASSERTION_ID = _REQUIREMENT["assertions"][0]["id"]
_ASSERTIONS = [
    {
        "id": _ASSERTION_ID,
        "kind": "viewport_contained",
        "locator": {"by": "test_id", "value": "mobile-toolbar"},
    }
]
_APPEARANCE_REQUIREMENT = normalize_visual_requirement(
    {
        "level": "surface",
        "target": "mobile toolbar",
        "assertions": ["toolbar has the requested balanced visual appearance"],
    }
)
_APPEARANCE_ID = _APPEARANCE_REQUIREMENT["assertions"][0]["id"]


class FakeSupervisor:
    def __init__(self, *, fingerprints=("same", "same"), contained=False):
        self.fingerprints = iter(fingerprints)
        self.contained = contained
        self.state_calls = 0

    def snapshot(self):
        return SimpleNamespace(active=True)

    def trusted_state_fingerprint(self, _locators):
        return next(self.fingerprints, "same")

    def trusted_element_state(self, _locator):
        self.state_calls += 1
        return {
            "ok": True,
            "count": 1,
            "exists": True,
            "visible": True,
            "viewport_contained": self.contained or self.state_calls > 1,
            "no_horizontal_overflow": True,
        }


@pytest.mark.asyncio
async def test_unrelated_exists_assertion_cannot_cover_overflow_requirement():
    requirement = normalize_visual_requirement(
        {
            "level": "surface",
            "target": "mobile toolbar",
            "assertions": ["toolbar has no horizontal overflow"],
        }
    )
    assertion_id = requirement["assertions"][0]["id"]

    result = await run_visual_assertions(
        task_id="coverage-mismatch",
        requirement=requirement,
        assertions=[
            {
                "id": assertion_id,
                "kind": "exists",
                "locator": {"by": "test_id", "value": "mobile-toolbar"},
            }
        ],
        supervisor=FakeSupervisor(contained=True),
    )

    assert result == {
        "status": "uncertain",
        "code": "invalid_visual_contract",
        "attempts": [],
    }


@pytest.mark.asyncio
async def test_exact_overflow_requirement_coverage_executes():
    requirement = normalize_visual_requirement(
        {
            "level": "surface",
            "target": "mobile toolbar",
            "assertions": ["toolbar has no horizontal overflow"],
        }
    )
    required = requirement["assertions"][0]

    result = await run_visual_assertions(
        task_id="coverage-match",
        requirement=requirement,
        assertions=[
            {
                "id": required["id"],
                "kind": required["kind"],
                "locator": {"by": "test_id", "value": "mobile-toolbar"},
            }
        ],
        supervisor=FakeSupervisor(contained=True),
    )

    assert required["kind"] == "no_horizontal_overflow"
    assert result["status"] == "passed"


@pytest.mark.asyncio
async def test_legacy_opaque_requirement_is_non_covering():
    legacy_requirement = {
        "level": "surface",
        "target": "vtarget_" + ("a" * 24),
        "assertions": ["vassert_" + ("b" * 24)],
    }

    result = await run_visual_assertions(
        task_id="legacy-coverage",
        requirement=legacy_requirement,
        assertions=[
            {
                "id": legacy_requirement["assertions"][0],
                "kind": "exists",
                "locator": {"by": "test_id", "value": "mobile-toolbar"},
            }
        ],
        supervisor=FakeSupervisor(contained=True),
    )

    assert result["code"] == "invalid_visual_contract"
    assert result["attempts"] == []


@pytest.mark.asyncio
async def test_resolves_hybrid_supervisor_by_effective_session_key(monkeypatch):
    from tools import browser_supervisor, browser_tool

    supervisor = FakeSupervisor(contained=True)
    seen = []
    monkeypatch.setattr(browser_tool, "_last_session_key", lambda _task: "task::local")
    monkeypatch.setattr(browser_tool, "_ensure_cdp_supervisor", lambda key: seen.append(key))
    monkeypatch.setattr(
        browser_supervisor.SUPERVISOR_REGISTRY,
        "get",
        lambda key: supervisor if key == "task::local" else None,
    )

    result = await run_visual_assertions(
        task_id="task",
        requirement=_REQUIREMENT,
        assertions=_ASSERTIONS,
    )

    assert result["status"] == "passed"
    assert seen == ["task::local"]


@pytest.mark.asyncio
async def test_no_retry_without_trusted_state_change():
    supervisor = FakeSupervisor(fingerprints=("same", "same"))

    result = await run_visual_assertions(
        task_id="task",
        requirement=_REQUIREMENT,
        assertions=_ASSERTIONS,
        supervisor=supervisor,
    )

    assert result["status"] == "failed"
    assert len(result["attempts"]) == 1
    assert supervisor.state_calls == 1


@pytest.mark.asyncio
async def test_exactly_one_retry_after_trusted_state_change():
    supervisor = FakeSupervisor(fingerprints=("before", "after", "after"))

    result = await run_visual_assertions(
        task_id="task",
        requirement=_REQUIREMENT,
        assertions=_ASSERTIONS,
        supervisor=supervisor,
    )

    assert result["status"] == "passed"
    assert len(result["attempts"]) == 2
    assert supervisor.state_calls == 2


@pytest.mark.asyncio
async def test_missing_browser_is_blocked_and_receipt_contains_no_protected_data():
    result = await run_visual_assertions(
        task_id="task",
        requirement=_REQUIREMENT,
        assertions=_ASSERTIONS,
        supervisor=SimpleNamespace(snapshot=lambda: SimpleNamespace(active=False)),
    )

    assert result["status"] == "blocked"
    receipt = result["visual_qa_receipt"]
    assert receipt["status"] == "blocked"
    assert set(receipt) == {
        "requirement_id",
        "contract_id",
        "assertion_ids",
        "status",
        "attempts",
        "vision_calls",
        "duration_ms",
        "diagnostic_codes",
    }
    assert "mobile-toolbar" not in repr(receipt)


@pytest.mark.asyncio
async def test_trusted_mutation_generation_allows_exactly_one_retry():
    class MutationSupervisor(FakeSupervisor):
        def trusted_element_state(self, locator):
            result = super().trusted_element_state(locator)
            if self.state_calls == 1:
                record_trusted_visual_mutation("mutation-task")
            return result

    supervisor = MutationSupervisor(fingerprints=("same", "same", "same"))

    result = await run_visual_assertions(
        task_id="mutation-task",
        requirement=_REQUIREMENT,
        assertions=_ASSERTIONS,
        supervisor=supervisor,
    )

    assert result["status"] == "passed"
    assert len(result["attempts"]) == 2


@pytest.mark.asyncio
async def test_timed_out_vision_attempt_consumes_single_provider_budget():
    class ScreenshotSupervisor(FakeSupervisor):
        def capture_screenshot_memory(self):
            return {"ok": True, "image_bytes": b"png"}

    calls = []

    async def evaluator(*_args, **_kwargs):
        calls.append(True)
        record_trusted_visual_mutation("vision-budget-task")
        await asyncio.sleep(2)
        return {"status": "passed", "results": []}

    result = await run_visual_assertions(
        task_id="vision-budget-task",
        requirement=_APPEARANCE_REQUIREMENT,
        assertions=[
            {
                "id": _APPEARANCE_ID,
                "kind": "screenshot_appearance",
                "expectation": "balanced layout",
            }
        ],
        supervisor=ScreenshotSupervisor(fingerprints=("same", "same", "same")),
        vision_evaluator=evaluator,
        config={
            "agent": {
                "visual_qa": {
                    "total_timeout_s": 2.0,
                    "attempt_timeout_s": 1.0,
                    "max_attempts": 2,
                    "max_vision_calls": 1,
                }
            }
        },
    )

    assert len(calls) == 1
    assert result["visual_qa_receipt"]["vision_calls"] == 1
    assert [attempt["vision_calls"] for attempt in result["attempts"]] == [1, 0]
    assert result["status"] != "passed"


@pytest.mark.asyncio
async def test_screenshot_failure_does_not_consume_provider_budget():
    class ScreenshotFailureSupervisor(FakeSupervisor):
        def capture_screenshot_memory(self):
            return {"ok": False, "error": "capture failed"}

    async def evaluator(*_args, **_kwargs):
        raise AssertionError("provider must not be invoked")

    result = await run_visual_assertions(
        task_id="screenshot-failure",
        requirement=_APPEARANCE_REQUIREMENT,
        assertions=[
            {"id": _APPEARANCE_ID, "kind": "screenshot_appearance", "expectation": "balanced layout"}
        ],
        supervisor=ScreenshotFailureSupervisor(),
        vision_evaluator=evaluator,
    )

    assert result["visual_qa_receipt"]["vision_calls"] == 0
    assert result["attempts"][0]["vision_calls"] == 0


@pytest.mark.asyncio
async def test_capability_rejection_before_provider_start_does_not_consume_budget():
    class ScreenshotSupervisor(FakeSupervisor):
        def capture_screenshot_memory(self):
            return {"ok": True, "image_bytes": b"png"}

    async def evaluator(*_args, on_provider_start, **_kwargs):
        return {
            "status": "uncertain",
            "results": [
                {"id": _APPEARANCE_ID, "status": "uncertain", "code": "vision_capability_unavailable"}
            ],
        }

    result = await run_visual_assertions(
        task_id="capability-block",
        requirement=_APPEARANCE_REQUIREMENT,
        assertions=[
            {"id": _APPEARANCE_ID, "kind": "screenshot_appearance", "expectation": "balanced layout"}
        ],
        supervisor=ScreenshotSupervisor(),
        vision_evaluator=evaluator,
    )

    assert result["visual_qa_receipt"]["vision_calls"] == 0


@pytest.mark.asyncio
async def test_pre_provider_timeout_does_not_consume_budget():
    class ScreenshotSupervisor(FakeSupervisor):
        def capture_screenshot_memory(self):
            return {"ok": True, "image_bytes": b"png"}

    async def evaluator(*_args, on_provider_start, **_kwargs):
        await asyncio.sleep(2)
        on_provider_start()
        return {"status": "passed", "results": []}

    result = await run_visual_assertions(
        task_id="pre-provider-timeout",
        requirement=_APPEARANCE_REQUIREMENT,
        assertions=[
            {"id": _APPEARANCE_ID, "kind": "screenshot_appearance", "expectation": "balanced layout"}
        ],
        supervisor=ScreenshotSupervisor(),
        vision_evaluator=evaluator,
        config={"agent": {"visual_qa": {"total_timeout_s": 1.0, "attempt_timeout_s": 1.0}}},
    )

    assert result["visual_qa_receipt"]["vision_calls"] == 0
    assert result["attempts"][0]["vision_calls"] == 0


@pytest.mark.asyncio
async def test_cancelled_sync_cdp_boundary_cannot_perform_late_side_effect():
    class GuardedSlowSupervisor(FakeSupervisor):
        def __init__(self):
            super().__init__()
            self.late_side_effects = 0

        def trusted_element_state(self, _locator, *, execution_guard=None):
            time.sleep(1.2)
            execution_guard.check()
            self.late_side_effects += 1
            return {"ok": True, "exists": True, "viewport_contained": True}

    supervisor = GuardedSlowSupervisor()
    result = await run_visual_assertions(
        task_id="late-cdp",
        requirement=_REQUIREMENT,
        assertions=_ASSERTIONS,
        supervisor=supervisor,
        config={"agent": {"visual_qa": {"total_timeout_s": 1.0, "attempt_timeout_s": 1.0}}},
    )
    await asyncio.sleep(0.3)

    assert result["status"] == "uncertain"
    assert supervisor.late_side_effects == 0


@pytest.mark.asyncio
async def test_external_caller_timeout_prevents_late_provider_start():
    class ScreenshotSupervisor(FakeSupervisor):
        def capture_screenshot_memory(self):
            return {"ok": True, "image_bytes": b"png"}

    provider_starts = []

    async def evaluator(*_args, on_provider_start, **_kwargs):
        await asyncio.to_thread(time.sleep, 0.2)
        on_provider_start()
        provider_starts.append(True)
        return {"status": "passed", "results": []}

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            run_visual_assertions(
                task_id="external-timeout",
                requirement=_APPEARANCE_REQUIREMENT,
                assertions=[
                    {"id": _APPEARANCE_ID, "kind": "screenshot_appearance", "expectation": "balanced layout"}
                ],
                supervisor=ScreenshotSupervisor(),
                vision_evaluator=evaluator,
            ),
            timeout=0.05,
        )
    await asyncio.sleep(0.3)

    assert provider_starts == []


def test_task_cleanup_discards_trusted_mutation_generation(monkeypatch):
    from tools import browser_tool

    task_id = "cleanup-task"
    record_trusted_visual_mutation(task_id)
    assert trusted_visual_mutation_token(task_id) == 1

    monkeypatch.setattr(browser_tool, "_cleanup_single_browser_session", lambda _task_id: None)
    browser_tool.cleanup_browser(task_id)

    assert trusted_visual_mutation_token(task_id) == 0


@pytest.mark.asyncio
async def test_total_deadline_bounds_fingerprint_operations():
    class SlowFingerprintSupervisor(FakeSupervisor):
        def trusted_state_fingerprint(self, _locators):
            time.sleep(2.0)
            return "late"

    started = time.monotonic()
    result = await run_visual_assertions(
        task_id="slow-task",
        requirement=_REQUIREMENT,
        assertions=_ASSERTIONS,
        supervisor=SlowFingerprintSupervisor(),
        config={
            "agent": {
                "visual_qa": {
                    "total_timeout_s": 1.0,
                    "attempt_timeout_s": 1.0,
                }
            }
        },
    )
    elapsed = time.monotonic() - started

    assert result["status"] == "uncertain"
    assert result["code"] == "total_timeout"
    assert elapsed < 1.3
