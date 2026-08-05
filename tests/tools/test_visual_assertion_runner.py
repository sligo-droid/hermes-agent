import asyncio
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.visual_qa import classify_visual_requirement, normalize_visual_requirement
from tools.visual_assertion_runner import (
    _acquire_viewport_scope,
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
        self.viewport_scope = threading.Lock()
        self.viewport_token = ""

    def begin_trusted_viewport_scope(self, _viewport=None, *, execution_guard=None):
        while not self.viewport_scope.acquire(timeout=0.01):
            if execution_guard is not None:
                execution_guard.check()
        if execution_guard is not None:
            try:
                execution_guard.check()
            except Exception:
                self.viewport_scope.release()
                raise
        self.viewport_token = f"lease-{id(self)}"
        return {"ok": True, "token": self.viewport_token, "previous": {}}

    def reapply_trusted_viewport_scope(self, token, _viewport):
        return {"ok": token == self.viewport_token}

    def end_trusted_viewport_scope(self, token, _previous):
        if token != self.viewport_token:
            return {"ok": False, "code": "viewport_scope_unavailable"}
        self.viewport_token = ""
        self.viewport_scope.release()
        return {"ok": True}

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
async def test_incident_contract_is_orchestrator_supplied_and_target_scoped():
    requirement = classify_visual_requirement(
        "in the Issue Attention graph in the State Brief page:\n"
        "-the bar graphs clip through the x axis\n"
        "-we should lightly label the y axis",
        worker_route="action",
    )
    contract = {
        "target": {
            "description": "Issue Attention graph region",
            "locator": {"by": "test_id", "value": "issue-attention-graph"},
        },
        "page": {
            "state": "prepared",
            "description": "State Brief page with Issue Attention visible",
        },
        "viewport": {
            "description": "current desktop viewport",
            "width": 1440,
            "height": 900,
        },
        "state": ["chart data loaded", "bars and both axes visible"],
        "assertions": [
            {
                "kind": "screenshot_appearance",
                "expectation": (
                    "Every rounded bar terminates above the x-axis; no filled polygon crosses "
                    "the baseline."
                ),
            },
            {
                "kind": "screenshot_appearance",
                "expectation": (
                    "Y-axis labels are visible and intentionally subtle without competing with "
                    "the bars."
                ),
            },
        ],
    }

    class IncidentSupervisor(FakeSupervisor):
        capture_calls = None

        def capture_screenshot_memory(self, *, locator=None, viewport=None):
            if self.capture_calls is None:
                self.capture_calls = []
            self.capture_calls.append({"locator": locator, "viewport": viewport})
            return {
                "ok": True,
                "image_bytes": b"focused-png" if locator else b"context-png",
            }

    supervisor = IncidentSupervisor(contained=True)
    seen_context = {}
    seen_images = []

    async def sweeper(images, *, on_provider_start, **_kwargs):
        seen_images.append(("sweep", list(images)))
        on_provider_start()
        return True

    async def evaluator(
        images,
        assertions,
        *,
        execution_context,
        on_provider_start,
        **_kwargs,
    ):
        seen_images.append(("inspect", list(images)))
        seen_context.update(execution_context)
        on_provider_start()
        return {
            "status": "passed",
            "results": [
                {
                    "id": item["id"],
                    "status": "passed",
                    "code": "appearance_satisfied",
                }
                for item in assertions
            ],
        }

    result = await run_visual_assertions(
        task_id="incident-contract",
        requirement=requirement,
        contract=contract,
        supervisor=supervisor,
        vision_sweeper=sweeper,
        vision_evaluator=evaluator,
    )

    assert result["status"] == "passed"
    assert supervisor.capture_calls == [
        {
            "locator": {"by": "test_id", "value": "issue-attention-graph"},
            "viewport": {"width": 1440, "height": 900},
        },
        {"locator": None, "viewport": {"width": 1440, "height": 900}},
    ]
    assert seen_images[0][0] == "sweep"
    assert seen_images[1] == ("inspect", seen_images[0][1])
    assert len(seen_images[0][1]) == 2
    assert seen_context == {
        "target": {"description": "Issue Attention graph region"},
        "page": contract["page"],
        "viewport": contract["viewport"],
        "state": contract["state"],
        "artifacts": [
            {
                "kind": "focused",
                "description": "Issue Attention graph region",
                "viewport": contract["viewport"],
            },
            {
                "kind": "context",
                "description": "Surrounding page context",
                "viewport": contract["viewport"],
            },
        ],
    }
    artifacts = result["screenshot_artifacts"]
    assert [item["kind"] for item in artifacts] == ["focused", "context"]
    assert [Path(item["screenshot_path"]).read_bytes() for item in artifacts] == [
        b"focused-png",
        b"context-png",
    ]
    receipt = result["visual_qa_receipt"]
    assert receipt["coverage_ids"] == [requirement["assertions"][0]["id"]]
    assert all(item.startswith("vassert_") for item in receipt["assertion_ids"])
    assert "Issue Attention" not in repr(receipt)
    assert "x-axis" not in repr(receipt)
    assert "screenshot_path" not in repr(receipt)


@pytest.mark.asyncio
async def test_screenshot_artifacts_do_not_turn_uncertain_inspection_into_success():
    requirement = classify_visual_requirement(
        "Make the dashboard chart visually balanced.",
        worker_route="action",
    )
    contract = {
        "target": {"description": "dashboard chart"},
        "page": {"state": "already_open", "description": "dashboard page"},
        "viewport": {"description": "current desktop viewport"},
        "state": ["chart data loaded"],
        "assertions": [
            {
                "kind": "screenshot_appearance",
                "expectation": "The chart is visually balanced.",
            }
        ],
    }

    class ScreenshotSupervisor(FakeSupervisor):
        def capture_screenshot_memory(self):
            return {"ok": True, "image_bytes": b"uncertain-evidence"}

    async def sweeper(*_args, on_provider_start, **_kwargs):
        on_provider_start()
        return True

    async def evaluator(_images, assertions, *, on_provider_start, **_kwargs):
        on_provider_start()
        return {
            "status": "uncertain",
            "results": [
                {
                    "id": item["id"],
                    "status": "uncertain",
                    "code": "appearance_uncertain",
                }
                for item in assertions
            ],
        }

    result = await run_visual_assertions(
        task_id="uncertain-artifact",
        requirement=requirement,
        contract=contract,
        supervisor=ScreenshotSupervisor(),
        vision_sweeper=sweeper,
        vision_evaluator=evaluator,
    )

    assert result["status"] == "uncertain"
    assert result["visual_qa_receipt"]["status"] == "uncertain"
    assert len(result["screenshot_artifacts"]) == 1
    assert Path(result["screenshot_artifacts"][0]["screenshot_path"]).read_bytes() == (
        b"uncertain-evidence"
    )


@pytest.mark.asyncio
async def test_orchestrated_requirement_rejects_assertions_without_semantic_contract():
    requirement = classify_visual_requirement(
        "Fix the Issue Attention chart on the State Brief page.",
        worker_route="action",
    )

    result = await run_visual_assertions(
        task_id="missing-orchestrator-contract",
        requirement=requirement,
        assertions=[
            {
                "kind": "screenshot_appearance",
                "expectation": "The chart looks correct.",
            }
        ],
        supervisor=FakeSupervisor(contained=True),
    )

    assert result["status"] == "uncertain"
    assert result["code"] == "invalid_visual_contract"
    assert result["reason_code"] == "contract_missing_fields"
    assert result["correction"] == (
        "Provide target, page, viewport, state, and at least one assertion."
    )
    assert result["attempts"] == []


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
        browser_tool,
        "_align_cdp_supervisor_to_current_page",
        lambda key, **_kwargs: seen.append(f"align:{key}"),
    )
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
    assert seen == ["task::local", "align:task::local"]


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

    async def sweeper(*_args, on_provider_start, **_kwargs):
        on_provider_start()
        return True

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
        vision_sweeper=sweeper,
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
    assert result["visual_qa_receipt"]["vision_calls"] == 2
    assert [attempt["vision_calls"] for attempt in result["attempts"]] == [2, 0]
    assert result["status"] != "passed"


@pytest.mark.asyncio
async def test_required_sweep_runs_before_sonnet_inspection():
    class ScreenshotSupervisor(FakeSupervisor):
        def capture_screenshot_memory(self):
            return {"ok": True, "image_bytes": b"png"}

    calls = []

    async def sweeper(*_args, on_provider_start, **_kwargs):
        calls.append("luna")
        on_provider_start()
        return True

    async def evaluator(*_args, on_provider_start, **_kwargs):
        calls.append("sonnet")
        on_provider_start()
        return {
            "status": "passed",
            "results": [
                {"id": _APPEARANCE_ID, "status": "passed", "code": "appearance_satisfied"}
            ],
        }

    result = await run_visual_assertions(
        task_id="mandatory-sweep",
        requirement=_APPEARANCE_REQUIREMENT,
        assertions=[
            {"id": _APPEARANCE_ID, "kind": "screenshot_appearance", "expectation": "balanced layout"}
        ],
        supervisor=ScreenshotSupervisor(),
        vision_sweeper=sweeper,
        vision_evaluator=evaluator,
        config={"agent": {"visual_qa": {"max_vision_calls": 2}}},
    )

    assert result["status"] == "passed"
    assert calls == ["luna", "sonnet"]
    assert result["visual_qa_receipt"]["vision_calls"] == 2


@pytest.mark.asyncio
async def test_custom_inspector_cannot_bypass_default_sweep(monkeypatch):
    class ScreenshotSupervisor(FakeSupervisor):
        def capture_screenshot_memory(self):
            return {"ok": True, "image_bytes": b"png"}

    calls = []

    async def default_sweeper(*_args, on_provider_start, **_kwargs):
        calls.append("luna")
        on_provider_start()
        return True

    async def evaluator(*_args, on_provider_start, **_kwargs):
        calls.append("sonnet")
        on_provider_start()
        return {
            "status": "passed",
            "results": [
                {"id": _APPEARANCE_ID, "status": "passed", "code": "appearance_satisfied"}
            ],
        }

    monkeypatch.setattr("agent.vision_assertions.run_visual_sweep", default_sweeper)
    result = await run_visual_assertions(
        task_id="default-sweep",
        requirement=_APPEARANCE_REQUIREMENT,
        assertions=[
            {"id": _APPEARANCE_ID, "kind": "screenshot_appearance", "expectation": "balanced layout"}
        ],
        supervisor=ScreenshotSupervisor(),
        vision_evaluator=evaluator,
        config={"agent": {"visual_qa": {"max_vision_calls": 2}}},
    )

    assert result["status"] == "passed"
    assert calls == ["luna", "sonnet"]


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

    async def sweeper(*_args, on_provider_start, **_kwargs):
        on_provider_start()
        return True

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
                vision_sweeper=sweeper,
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


def _mixed_contract():
    return {
        "target": {
            "description": "mobile toolbar",
            "locator": {"by": "test_id", "value": "mobile-toolbar"},
        },
        "page": {"state": "already_open", "description": "mobile page"},
        "viewport": {"description": "mobile viewport"},
        "state": ["toolbar rendered"],
        "assertions": [
            {
                "kind": "viewport_contained",
                "locator": {"by": "test_id", "value": "mobile-toolbar"},
            },
            {"kind": "screenshot_appearance", "expectation": "Toolbar is visually balanced."},
        ],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "appearance_results",
    [
        [],
        [{"id": "wrong", "status": "passed", "code": "appearance_satisfied"}],
        [
            {"id": "duplicate", "status": "passed", "code": "appearance_satisfied"},
            {"id": "duplicate", "status": "passed", "code": "appearance_satisfied"},
        ],
        ["malformed"],
    ],
)
async def test_custom_evaluator_cannot_bypass_exact_appearance_coverage(appearance_results):
    requirement = classify_visual_requirement(
        "Make the mobile toolbar fit and look balanced.", worker_route="action"
    )

    class ScreenshotSupervisor(FakeSupervisor):
        def capture_screenshot_memory(self, **_kwargs):
            return {"ok": True, "image_bytes": b"png"}

    async def sweeper(*_args, on_provider_start, **_kwargs):
        on_provider_start()
        return True

    async def evaluator(*_args, on_provider_start, **_kwargs):
        on_provider_start()
        return {"status": "passed", "results": appearance_results}

    result = await run_visual_assertions(
        task_id="invalid-custom-coverage",
        requirement=requirement,
        contract=_mixed_contract(),
        supervisor=ScreenshotSupervisor(contained=True),
        vision_sweeper=sweeper,
        vision_evaluator=evaluator,
    )

    assert result["status"] == "uncertain"
    assert {item["status"] for item in result["results"]} == {"passed", "uncertain"}
    assert "invalid_vision_output" in result["visual_qa_receipt"]["diagnostic_codes"]


@pytest.mark.asyncio
async def test_passing_vision_cannot_override_deterministic_failure():
    requirement = classify_visual_requirement(
        "Make the mobile toolbar fit and look balanced.", worker_route="action"
    )

    class ScreenshotSupervisor(FakeSupervisor):
        def capture_screenshot_memory(self, **_kwargs):
            return {"ok": True, "image_bytes": b"png"}

    async def sweeper(*_args, on_provider_start, **_kwargs):
        on_provider_start()
        return True

    async def evaluator(_images, assertions, *, on_provider_start, **_kwargs):
        on_provider_start()
        return {
            "status": "passed",
            "results": [
                {"id": item["id"], "status": "passed", "code": "appearance_satisfied"}
                for item in assertions
            ],
        }

    result = await run_visual_assertions(
        task_id="deterministic-precedence",
        requirement=requirement,
        contract=_mixed_contract(),
        supervisor=ScreenshotSupervisor(contained=False),
        vision_sweeper=sweeper,
        vision_evaluator=evaluator,
    )

    assert result["status"] == "failed"
    assert "viewport_contained_mismatch" in result["visual_qa_receipt"]["diagnostic_codes"]


@pytest.mark.asyncio
async def test_viewport_apply_failure_is_blocked_before_assertions_or_vision():
    requirement = classify_visual_requirement(
        "Make the mobile toolbar visually balanced.", worker_route="action"
    )
    contract = _mixed_contract()
    contract["viewport"].update({"width": 390, "height": 844})
    calls = []

    class ScopeFailureSupervisor(FakeSupervisor):
        def begin_trusted_viewport_scope(self, _viewport, *, execution_guard=None):
            return {"ok": False, "code": "viewport_apply_unverified"}

        def reapply_trusted_viewport_scope(self, _token, _viewport):
            raise AssertionError("reapply must not run after scope failure")

        def end_trusted_viewport_scope(self, _token, _previous):
            raise AssertionError("restore must not run after scope failure")

        def trusted_element_state(self, _locator):
            calls.append("deterministic")
            return super().trusted_element_state(_locator)

    async def evaluator(*_args, **_kwargs):
        calls.append("vision")
        return {"status": "passed", "results": []}

    result = await run_visual_assertions(
        task_id="viewport-apply-failure",
        requirement=requirement,
        contract=contract,
        supervisor=ScopeFailureSupervisor(contained=True),
        vision_evaluator=evaluator,
    )

    assert result["status"] == "blocked"
    assert result["code"] == "viewport_apply_unverified"
    assert calls == []


@pytest.mark.asyncio
async def test_restore_failure_downgrades_passing_run():
    requirement = classify_visual_requirement(
        "Make the dashboard chart visually balanced.", worker_route="action"
    )
    contract = {
        "target": {"description": "dashboard chart"},
        "page": {"state": "already_open", "description": "dashboard page"},
        "viewport": {"description": "mobile viewport", "width": 390, "height": 844},
        "state": ["chart loaded"],
        "assertions": [
            {"kind": "screenshot_appearance", "expectation": "Chart is visually balanced."}
        ],
    }

    class RestoreFailureSupervisor(FakeSupervisor):
        def begin_trusted_viewport_scope(self, _viewport, *, execution_guard=None):
            return super().begin_trusted_viewport_scope(
                _viewport,
                execution_guard=execution_guard,
            )

        def reapply_trusted_viewport_scope(self, _token, _viewport):
            return {"ok": True}

        def end_trusted_viewport_scope(self, _token, _previous):
            self.viewport_token = ""
            self.viewport_scope.release()
            return {"ok": False, "code": "viewport_restore_unverified"}

        def capture_screenshot_memory(self, **_kwargs):
            return {"ok": True, "image_bytes": b"png"}

    async def sweeper(*_args, on_provider_start, **_kwargs):
        on_provider_start()
        return True

    async def evaluator(_images, assertions, *, on_provider_start, **_kwargs):
        on_provider_start()
        return {
            "status": "passed",
            "results": [
                {"id": item["id"], "status": "passed", "code": "appearance_satisfied"}
                for item in assertions
            ],
        }

    result = await run_visual_assertions(
        task_id="restore-failure",
        requirement=requirement,
        contract=contract,
        supervisor=RestoreFailureSupervisor(),
        vision_sweeper=sweeper,
        vision_evaluator=evaluator,
    )

    assert result["status"] == "uncertain"
    assert result["code"] == "viewport_restore_unverified"
    assert "viewport_restore_unverified" in result["visual_qa_receipt"]["diagnostic_codes"]


def test_supervisor_viewport_scope_serializes_concurrent_callers():
    from tools.browser_supervisor import CDPSupervisor

    supervisor = CDPSupervisor("scope-test", "ws://127.0.0.1/test")
    events = []
    first_acquired = threading.Event()
    release_first = threading.Event()

    def state(**_kwargs):
        return {"ok": True, "width": 1280, "height": 720, "deviceScaleFactor": 1.0}

    supervisor._effective_viewport_state = state
    supervisor._set_trusted_viewport_override = lambda *_args, **_kwargs: True
    supervisor._verify_trusted_viewport = lambda *_args, **_kwargs: True

    def worker(name, wait=False):
        scope = supervisor.begin_trusted_viewport_scope()
        events.append(f"{name}:acquire")
        if wait:
            first_acquired.set()
            release_first.wait(timeout=2)
        events.append(f"{name}:restore")
        supervisor.end_trusted_viewport_scope(scope["token"], scope["previous"])
        events.append(f"{name}:release")

    first = threading.Thread(target=worker, args=("a", True))
    second = threading.Thread(target=worker, args=("b",))
    first.start()
    assert first_acquired.wait(timeout=2)
    second.start()
    time.sleep(0.05)
    assert events == ["a:acquire"]
    release_first.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert events == [
        "a:acquire",
        "a:restore",
        "a:release",
        "b:acquire",
        "b:restore",
        "b:release",
    ]


@pytest.mark.asyncio
async def test_viewport_scope_timeout_leaves_no_late_orphan_state():
    from agent.execution_guard import CooperativeExecutionGuard
    from tools.browser_supervisor import CDPSupervisor

    supervisor = CDPSupervisor("timeout-scope", "ws://127.0.0.1/test")
    supervisor._effective_viewport_state = lambda **_kwargs: {
        "ok": True,
        "width": 1280,
        "height": 720,
        "deviceScaleFactor": 1.0,
    }
    mutations = []
    supervisor._set_trusted_viewport_override = (
        lambda override, **_kwargs: mutations.append(override) or True
    )
    supervisor._verify_trusted_viewport = lambda *_args, **_kwargs: True
    assert supervisor._viewport_scope_lock.acquire(timeout=0.1)
    guard = CooperativeExecutionGuard(time.monotonic() + 0.05)

    result = await _acquire_viewport_scope(
        supervisor,
        {"width": 390, "height": 844},
        deadline=time.monotonic() + 0.05,
        execution_guard=guard,
    )
    supervisor._viewport_scope_lock.release()
    await asyncio.sleep(0.1)

    assert result == {"ok": False, "code": "viewport_scope_unavailable"}
    assert supervisor._viewport_scope_token is None
    assert supervisor._trusted_viewport_override is None
    assert mutations == []
    assert supervisor._viewport_scope_lock.acquire(timeout=0.1)
    supervisor._viewport_scope_lock.release()


@pytest.mark.asyncio
async def test_viewport_scope_cancellation_leaves_no_late_orphan_state():
    from agent.execution_guard import CooperativeExecutionGuard
    from tools.browser_supervisor import CDPSupervisor

    supervisor = CDPSupervisor("cancel-scope", "ws://127.0.0.1/test")
    supervisor._effective_viewport_state = lambda **_kwargs: {
        "ok": True,
        "width": 1280,
        "height": 720,
        "deviceScaleFactor": 1.0,
    }
    mutations = []
    supervisor._set_trusted_viewport_override = (
        lambda override, **_kwargs: mutations.append(override) or True
    )
    supervisor._verify_trusted_viewport = lambda *_args, **_kwargs: True
    assert supervisor._viewport_scope_lock.acquire(timeout=0.1)
    guard = CooperativeExecutionGuard(time.monotonic() + 2)
    acquisition = asyncio.create_task(
        _acquire_viewport_scope(
            supervisor,
            {"width": 390, "height": 844},
            deadline=time.monotonic() + 2,
            execution_guard=guard,
        )
    )
    await asyncio.sleep(0.05)
    acquisition.cancel()
    with pytest.raises(asyncio.CancelledError):
        await acquisition
    supervisor._viewport_scope_lock.release()
    await asyncio.sleep(0.1)

    assert supervisor._viewport_scope_token is None
    assert supervisor._trusted_viewport_override is None
    assert mutations == []
    assert supervisor._viewport_scope_lock.acquire(timeout=0.1)
    supervisor._viewport_scope_lock.release()


@pytest.mark.asyncio
async def test_ambient_visual_qa_serializes_against_concrete_visual_qa():
    requirement = classify_visual_requirement(
        "Make the dashboard chart visually balanced.", worker_route="action"
    )
    ambient_contract = {
        "target": {"description": "dashboard chart"},
        "page": {"state": "already_open", "description": "dashboard page"},
        "viewport": {"description": "current desktop viewport"},
        "state": ["chart loaded"],
        "assertions": [
            {"kind": "screenshot_appearance", "expectation": "Chart is visually balanced."}
        ],
    }
    concrete_contract = {
        **ambient_contract,
        "viewport": {"description": "mobile viewport", "width": 390, "height": 844},
    }
    events = []
    first_capture = threading.Event()
    release_first = threading.Event()

    class SerializedSupervisor(FakeSupervisor):
        def begin_trusted_viewport_scope(self, viewport=None, *, execution_guard=None):
            result = super().begin_trusted_viewport_scope(
                viewport,
                execution_guard=execution_guard,
            )
            events.append("ambient:acquire" if viewport is None else "concrete:acquire")
            return result

        def end_trusted_viewport_scope(self, token, previous):
            events.append("release")
            return super().end_trusted_viewport_scope(token, previous)

        def capture_screenshot_memory(self, **_kwargs):
            if not first_capture.is_set():
                first_capture.set()
                release_first.wait(timeout=2)
            return {"ok": True, "image_bytes": b"png"}

    supervisor = SerializedSupervisor(contained=True)

    async def sweeper(*_args, on_provider_start, **_kwargs):
        on_provider_start()
        return True

    async def evaluator(_images, assertions, *, on_provider_start, **_kwargs):
        on_provider_start()
        return {
            "status": "passed",
            "results": [
                {"id": item["id"], "status": "passed", "code": "appearance_satisfied"}
                for item in assertions
            ],
        }

    ambient = asyncio.create_task(
        run_visual_assertions(
            task_id="ambient-serialized",
            requirement=requirement,
            contract=ambient_contract,
            supervisor=supervisor,
            vision_sweeper=sweeper,
            vision_evaluator=evaluator,
        )
    )
    assert await asyncio.to_thread(first_capture.wait, 2)
    concrete = asyncio.create_task(
        run_visual_assertions(
            task_id="concrete-serialized",
            requirement=requirement,
            contract=concrete_contract,
            supervisor=supervisor,
            vision_sweeper=sweeper,
            vision_evaluator=evaluator,
        )
    )
    await asyncio.sleep(0.05)
    assert events == ["ambient:acquire"]
    release_first.set()
    ambient_result, concrete_result = await asyncio.gather(ambient, concrete)

    assert ambient_result["status"] == "passed"
    assert concrete_result["status"] == "passed"
    assert events == ["ambient:acquire", "release", "concrete:acquire", "release"]


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
