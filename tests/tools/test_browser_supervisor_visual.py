import threading
import time
from unittest.mock import patch

import pytest

from tools.browser_supervisor import CDPSupervisor


def _supervisor():
    return CDPSupervisor("task-1", "ws://127.0.0.1/devtools/browser/test")


def test_diagnostic_cursor_and_safe_codes_do_not_expose_console_payload():
    supervisor = _supervisor()
    cursor = supervisor.diagnostic_cursor()

    supervisor._on_console(
        {"type": "error", "args": [{"value": "secret page payload"}]},
        level_from="api",
    )
    supervisor._on_console(
        {"exceptionDetails": {"text": "token=private", "url": "https://private.test"}},
        level_from="exception",
    )

    result = supervisor.diagnostics_since(cursor)
    assert result["ok"] is True
    assert result["cursor"].startswith("dcur_2_")
    assert result["codes"] == ["console_error", "runtime_exception"]
    assert "secret" not in repr(result)
    assert "private.test" not in repr(result)


def test_model_supplied_future_or_forged_cursor_fails_closed():
    supervisor = _supervisor()

    forged = supervisor.diagnostics_since("dcur_999_0123456789abcdef01234567")

    assert forged["ok"] is False
    assert forged["codes"] == ["invalid_diagnostic_cursor"]
    assert forged["cursor"].startswith("dcur_0_")


def test_valid_cursor_predating_evicted_diagnostic_history_is_uncertain():
    supervisor = _supervisor()
    old_cursor = supervisor.diagnostic_cursor()
    for index in range(101):
        supervisor._on_console(
            {"type": "error", "args": [{"value": f"diagnostic-{index}"}]},
            level_from="api",
        )

    result = supervisor.diagnostics_since(old_cursor)

    assert result["ok"] is False
    assert result["codes"] == ["diagnostic_history_evicted"]
    assert result["truncated"] is True
    assert result["cursor"].startswith("dcur_101_")


def test_trusted_element_state_returns_host_authored_facts_only():
    supervisor = _supervisor()
    with patch.object(
        supervisor,
        "evaluate_runtime",
        return_value={
            "ok": True,
            "result": {
                "count": 1,
                "exists": True,
                "visible": True,
                "viewport_contained": True,
                "no_horizontal_overflow": True,
                "bounds": {"x": 1, "y": 2, "width": 300, "height": 40},
            },
        },
    ) as evaluate:
        state = supervisor.trusted_element_state(
            {"by": "test_id", "value": "mobile-toolbar"}
        )

    assert state["ok"] is True
    assert state["visible"] is True
    expression = evaluate.call_args.args[0]
    assert "Runtime.evaluate" not in expression
    assert "fetch(" not in expression


def test_trusted_locator_rejects_javascript_shaped_css():
    supervisor = _supervisor()

    state = supervisor.trusted_element_state(
        {"by": "css", "value": "div; fetch('/secret')"}
    )

    assert state == {"ok": False, "error": "invalid trusted locator"}


def test_state_fingerprint_contains_no_locator_material():
    supervisor = _supervisor()
    with patch.object(
        supervisor,
        "trusted_element_state",
        return_value={"ok": True, "count": 1, "exists": True},
    ), patch.object(supervisor, "snapshot") as snapshot:
        snapshot.return_value.active = True
        fingerprint = supervisor.trusted_state_fingerprint(
            [{"by": "test_id", "value": "protected-selector"}]
        )

    assert len(fingerprint) == 24
    assert "protected-selector" not in fingerprint


def test_target_screenshot_captures_host_authored_locator_beyond_viewport():
    supervisor = _supervisor()
    with patch.object(
        supervisor,
        "trusted_element_state",
        return_value={
            "ok": True,
            "visible": True,
            "bounds": {
                "page_x": 20,
                "page_y": 40,
                "width": 640,
                "height": 360,
            },
        },
    ), patch.object(
        supervisor,
        "_page_cdp_call",
        return_value={
            "ok": True,
            "response": {"result": {"data": "cG5n"}},
        },
    ) as cdp_call:
        result = supervisor.capture_screenshot_memory(
            locator={"by": "test_id", "value": "issue-attention-graph"}
        )

    assert result == {
        "ok": True,
        "image_bytes": b"png",
        "mime_type": "image/png",
    }
    assert cdp_call.call_args.args == (
        "Page.captureScreenshot",
        {
            "format": "png",
            "fromSurface": True,
            "captureBeyondViewport": True,
            "clip": {
                "x": 20.0,
                "y": 40.0,
                "width": 640.0,
                "height": 360.0,
                "scale": 1,
            },
        },
    )


@pytest.mark.parametrize(
    "bounds",
    [
        {"page_x": 0, "page_y": 0, "width": 5000, "height": 5000},
        {"page_x": float("inf"), "page_y": 0, "width": 640, "height": 360},
    ],
)
def test_target_screenshot_rejects_unsafe_locator_clip(bounds):
    supervisor = _supervisor()
    with patch.object(
        supervisor,
        "trusted_element_state",
        return_value={"ok": True, "visible": True, "bounds": bounds},
    ), patch.object(supervisor, "_page_cdp_call") as cdp_call:
        result = supervisor.capture_screenshot_memory(
            locator={"by": "test_id", "value": "issue-attention-graph"}
        )

    assert result == {"ok": False, "error": "target screenshot bounds unavailable"}
    cdp_call.assert_not_called()


def test_responsive_screenshot_uses_bounded_viewport_and_restores_it():
    supervisor = _supervisor()
    calls = []

    def cdp_call(method, params, **_kwargs):
        calls.append((method, params))
        if method == "Page.captureScreenshot":
            return {"ok": True, "response": {"result": {"data": "cG5n"}}}
        return {"ok": True, "response": {"result": {}}}

    with patch.object(
        supervisor,
        "_effective_viewport_state",
        side_effect=[
            {"ok": True, "width": 1280, "height": 720, "deviceScaleFactor": 1.0},
            {"ok": True, "width": 390, "height": 844, "deviceScaleFactor": 1.0},
            {"ok": True, "width": 1280, "height": 720, "deviceScaleFactor": 1.0},
        ],
    ), patch.object(supervisor, "_page_cdp_call", side_effect=cdp_call):
        result = supervisor.capture_screenshot_memory(
            viewport={"width": 390, "height": 844}
        )

    assert result["image_bytes"] == b"png"
    assert calls == [
        (
            "Emulation.setDeviceMetricsOverride",
            {
                "width": 390,
                "height": 844,
                "deviceScaleFactor": 1,
                "mobile": False,
            },
        ),
        (
            "Page.captureScreenshot",
            {
                "format": "png",
                "fromSurface": True,
                "captureBeyondViewport": False,
            },
        ),
        ("Emulation.clearDeviceMetricsOverride", {}),
    ]


def test_viewport_scope_restores_exact_preexisting_override_and_verifies_it():
    supervisor = _supervisor()
    previous = {
        "width": 1024,
        "height": 768,
        "deviceScaleFactor": 2,
        "mobile": True,
        "screenOrientation": {"type": "portraitPrimary", "angle": 0},
    }
    supervisor._trusted_viewport_override = dict(previous)
    calls = []

    def cdp_call(method, params, **_kwargs):
        calls.append((method, params))
        return {"ok": True, "response": {"result": {}}}

    with patch.object(
        supervisor,
        "_effective_viewport_state",
        side_effect=[
            {"ok": True, "width": 1024, "height": 768, "deviceScaleFactor": 2.0},
            {"ok": True, "width": 390, "height": 844, "deviceScaleFactor": 1.0},
            {"ok": True, "width": 1024, "height": 768, "deviceScaleFactor": 2.0},
        ],
    ), patch.object(supervisor, "_page_cdp_call", side_effect=cdp_call):
        scope = supervisor.begin_trusted_viewport_scope({"width": 390, "height": 844})
        restored = supervisor.end_trusted_viewport_scope(
            scope["token"], scope["previous"]
        )

    assert scope["ok"] is True
    assert restored == {"ok": True}
    assert calls == [
        (
            "Emulation.setDeviceMetricsOverride",
            {"width": 390, "height": 844, "deviceScaleFactor": 1, "mobile": False},
        ),
        ("Emulation.setDeviceMetricsOverride", previous),
    ]
    assert supervisor._trusted_viewport_override == previous


def test_post_apply_verification_exception_reports_failed_restoration():
    supervisor = _supervisor()
    previous = {
        "width": 1024,
        "height": 768,
        "deviceScaleFactor": 2,
        "mobile": True,
    }
    requested = {
        "width": 390,
        "height": 844,
        "deviceScaleFactor": 1,
        "mobile": False,
    }
    supervisor._trusted_viewport_override = dict(previous)
    set_calls = []
    ownership_during_restore = []

    def set_override(override, **_kwargs):
        set_calls.append(override)
        if len(set_calls) == 1:
            supervisor._trusted_viewport_override = dict(override)
            return True
        ownership_during_restore.append(
            (supervisor._viewport_scope_token is not None, supervisor._viewport_scope_lock.locked())
        )
        return False

    with patch.object(
        supervisor,
        "_effective_viewport_state",
        return_value={"ok": True, "width": 1024, "height": 768, "deviceScaleFactor": 2.0},
    ), patch.object(
        supervisor,
        "_set_trusted_viewport_override",
        side_effect=set_override,
    ), patch.object(
        supervisor,
        "_verify_trusted_viewport",
        side_effect=RuntimeError("post-apply verification failed"),
    ):
        result = supervisor.begin_trusted_viewport_scope({"width": 390, "height": 844})

    assert result == {"ok": False, "code": "viewport_restore_unavailable"}
    assert set_calls == [requested, previous]
    assert ownership_during_restore == [(True, True)]
    assert supervisor._trusted_viewport_override == requested
    assert supervisor._viewport_scope_token is None
    assert supervisor._viewport_scope_lock.acquire(timeout=0.1)
    supervisor._viewport_scope_lock.release()


def test_post_apply_verification_exception_reports_unverified_restoration():
    supervisor = _supervisor()
    previous = {
        "width": 1024,
        "height": 768,
        "deviceScaleFactor": 2,
        "mobile": True,
    }
    requested = {
        "width": 390,
        "height": 844,
        "deviceScaleFactor": 1,
        "mobile": False,
    }
    supervisor._trusted_viewport_override = dict(previous)
    set_calls = []
    verify_calls = 0
    ownership_during_restore = []

    def set_override(override, **_kwargs):
        set_calls.append(override)
        supervisor._trusted_viewport_override = dict(override)
        return True

    def verify(_expected, **_kwargs):
        nonlocal verify_calls
        verify_calls += 1
        if verify_calls == 1:
            raise RuntimeError("post-apply verification failed")
        ownership_during_restore.append(
            (supervisor._viewport_scope_token is not None, supervisor._viewport_scope_lock.locked())
        )
        return False

    with patch.object(
        supervisor,
        "_effective_viewport_state",
        return_value={"ok": True, "width": 1024, "height": 768, "deviceScaleFactor": 2.0},
    ), patch.object(
        supervisor,
        "_set_trusted_viewport_override",
        side_effect=set_override,
    ), patch.object(supervisor, "_verify_trusted_viewport", side_effect=verify):
        result = supervisor.begin_trusted_viewport_scope({"width": 390, "height": 844})

    assert result == {"ok": False, "code": "viewport_restore_unverified"}
    assert set_calls == [requested, previous]
    assert ownership_during_restore == [(True, True)]
    assert supervisor._trusted_viewport_override == previous
    assert supervisor._viewport_scope_token is None
    assert supervisor._viewport_scope_lock.acquire(timeout=0.1)
    supervisor._viewport_scope_lock.release()


def test_ambient_viewport_scope_serializes_standalone_responsive_capture():
    supervisor = _supervisor()
    calls = []
    capture_started = threading.Event()
    capture_done = threading.Event()

    def effective(**_kwargs):
        override = supervisor._trusted_viewport_override
        if override is not None:
            return {
                "ok": True,
                "width": override["width"],
                "height": override["height"],
                "deviceScaleFactor": override["deviceScaleFactor"],
            }
        return {"ok": True, "width": 1280, "height": 720, "deviceScaleFactor": 1.0}

    def cdp_call(method, params, **_kwargs):
        calls.append((method, params))
        if method == "Page.captureScreenshot":
            return {"ok": True, "response": {"result": {"data": "cG5n"}}}
        return {"ok": True, "response": {"result": {}}}

    with patch.object(supervisor, "_effective_viewport_state", side_effect=effective), patch.object(
        supervisor, "_page_cdp_call", side_effect=cdp_call
    ):
        ambient = supervisor.begin_trusted_viewport_scope()

        def capture():
            capture_started.set()
            supervisor.capture_screenshot_memory(viewport={"width": 390, "height": 844})
            capture_done.set()

        worker = threading.Thread(target=capture)
        worker.start()
        assert capture_started.wait(timeout=1)
        time.sleep(0.05)
        assert capture_done.is_set() is False
        assert calls == []
        assert supervisor.end_trusted_viewport_scope(
            ambient["token"], ambient["previous"]
        ) == {"ok": True}
        worker.join(timeout=2)

    assert capture_done.is_set() is True
    assert any(method == "Page.captureScreenshot" for method, _params in calls)


def test_responsive_screenshot_rejects_out_of_bounds_viewport_without_cdp():
    supervisor = _supervisor()
    with patch.object(supervisor, "_page_cdp_call") as cdp_call:
        result = supervisor.capture_screenshot_memory(
            viewport={"width": 100, "height": 844}
        )

    assert result == {"ok": False, "error": "invalid trusted viewport"}
    cdp_call.assert_not_called()
