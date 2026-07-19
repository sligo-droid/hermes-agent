from unittest.mock import patch

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
