import json
from unittest.mock import patch

from agent.preview_readiness import (
    MAX_PREVIEW_EVENTS,
    PreviewReadinessController,
    classify_preview_failure,
    record_preview_event,
    summarize_preview_events,
)
from agent.runtime_breakdown import build_turn_runtime_breakdown
from gateway.work_ledger import _durable_runtime_breakdown


def _frontend_repo(tmp_path):
    dashboard = tmp_path / "dashboard"
    dashboard.mkdir()
    (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
    (dashboard / "package.json").write_text(
        json.dumps({
            "scripts": {
                "dev": "vite dev",
                "build": "vite build",
                "preview": "vite preview",
                "qa:auth": "node scripts/authenticated-qa-smoke.mjs",
                "qa:with-env": "node scripts/with-env.mjs",
            }
        })
    )
    return tmp_path


def _start(controller, root, session_id="proc_dev", command=None):
    command = command or "pnpm --dir dashboard dev --host 127.0.0.1 --port 5174"
    args = {
        "command": command,
        "background": True,
        "workdir": str(root),
    }
    result, event = controller.after_call(
        "terminal",
        args,
        json.dumps({
            "output": "Background process started",
            "session_id": session_id,
            "exit_code": 0,
        }),
        session_cwd=str(root),
        visual_required=False,
    )
    assert event is None
    return result


def test_preview_failure_classifier_distinguishes_deterministic_and_transient_failures():
    vite = classify_preview_failure({
        "output_preview": "The request url /shared/pkg is outside of Vite serving allow list."
    })
    missing = classify_preview_failure({"output": "Error: supabaseUrl is required."})
    internal = classify_preview_failure({
        "title": "Internal Error",
        "snapshot": 'heading "Internal Error"',
    })
    hmr = classify_preview_failure({
        "error": "Vite server connection lost after redirect to cloudflareaccess.com"
    })
    ordinary_reload = classify_preview_failure({
        "error": "browser login page reloaded before authentication completed"
    })
    transient = classify_preview_failure({
        "error": "net::ERR_CONNECTION_REFUSED while navigating"
    })

    assert (vite.failure_class, vite.deterministic) == ("vite_fs_allow", True)
    assert (missing.failure_class, missing.deterministic) == (
        "missing_environment",
        True,
    )
    assert (internal.failure_class, internal.deterministic) == (
        "application_bootstrap",
        True,
    )
    assert (hmr.failure_class, hmr.deterministic) == (
        "hmr_origin_mismatch",
        True,
    )
    assert ordinary_reload is None
    assert (transient.failure_class, transient.deterministic) == (
        "transient_browser_network",
        False,
    )


def test_live_optional_visual_sequence_is_bounded_and_guides_native_launcher(tmp_path):
    root = _frontend_repo(tmp_path)
    controller = PreviewReadinessController()
    _start(controller, root)

    process_result = json.dumps({
        "status": "running",
        "session_id": "proc_dev",
        "output_preview": (
            "/home/private/worktree/node_modules/pkg is outside of Vite serving allow list. "
            "PASSWORD=do-not-retain"
        ),
    })
    with patch(
        "tools.process_registry.process_registry.kill_process",
        return_value={"status": "killed", "output": "PASSWORD=do-not-retain"},
    ) as kill:
        enriched, failure_event = controller.after_call(
            "process",
            {"action": "poll", "session_id": "proc_dev"},
            process_result,
            session_cwd=str(root),
            visual_required=False,
        )

    kill.assert_called_once_with(
        "proc_dev",
        source="preview_readiness",
        consume_output=False,
        suppress_completion=True,
    )
    assert failure_event["failure_class"] == "vite_fs_allow"
    assert failure_event["cleanup"]["status"] == "killed"
    assert failure_event["recommended_launcher"] == "pnpm --dir dashboard qa:auth"
    assert "/home/private" not in json.dumps(failure_event)
    assert "do-not-retain" not in json.dumps(failure_event)
    assert json.loads(enriched)["preview_readiness"]["failure_class"] == "vite_fs_allow"

    preview_block = controller.before_call(
        "terminal",
        {
            "command": "pnpm --dir dashboard preview --host 127.0.0.1 --port 5174",
            "background": True,
            "workdir": str(root),
        },
        session_cwd=str(root),
        visual_required=False,
    )
    assert preview_block.code == "preview_local_budget_exhausted"
    assert "Continue verification and closeout" in preview_block.message

    build_block = controller.before_call(
        "terminal",
        {"command": "pnpm --dir dashboard build", "workdir": str(root)},
        session_cwd=str(root),
        visual_required=False,
    )
    assert build_block.code == "preview_build_recovery_exhausted"

    browser_block = controller.before_call(
        "browser_navigate",
        {"url": "http://127.0.0.1:5174/races/239"},
        session_cwd=str(root),
        visual_required=False,
    )
    assert browser_block.code == "preview_failed_browser_target_blocked"

    native_args = {
        "command": "pnpm --dir dashboard qa:auth",
        "workdir": str(root),
    }
    assert (
        controller.before_call(
            "terminal",
            native_args,
            session_cwd=str(root),
            visual_required=False,
        )
        is None
    )
    native_result, native_event = controller.after_call(
        "terminal",
        native_args,
        json.dumps({"output": "QA passed", "exit_code": 0}),
        session_cwd=str(root),
        visual_required=False,
    )
    assert native_event["status"] == "ready"
    assert "not a visual-QA receipt" in native_event["summary"]
    assert json.loads(native_result)["preview_readiness"]["status"] == "ready"

    assert (
        controller.before_call(
            "terminal",
            {
                "command": "bash scripts/local_lifecycle/closeout.sh",
                "workdir": str(root),
            },
            session_cwd=str(root),
            visual_required=False,
        )
        is None
    )


def test_required_visual_qa_allows_two_local_strategies_and_one_native_launcher(
    tmp_path,
):
    root = _frontend_repo(tmp_path)
    controller = PreviewReadinessController()
    _start(controller, root, session_id="proc_dev")
    with patch(
        "tools.process_registry.process_registry.kill_process",
        return_value={"status": "killed"},
    ):
        controller.after_call(
            "process",
            {"action": "poll", "session_id": "proc_dev"},
            json.dumps({
                "status": "running",
                "output_preview": "outside of Vite serving allow list",
            }),
            session_cwd=str(root),
            visual_required=True,
        )

    preview_args = {
        "command": "pnpm --dir dashboard preview --host 127.0.0.1 --port 5174",
        "background": True,
        "workdir": str(root),
    }
    assert (
        controller.before_call(
            "terminal", preview_args, session_cwd=str(root), visual_required=True
        )
        is None
    )
    controller.after_call(
        "terminal",
        preview_args,
        json.dumps({
            "output": "Background process started",
            "session_id": "proc_preview",
            "exit_code": 0,
        }),
        session_cwd=str(root),
        visual_required=True,
    )
    with patch(
        "tools.process_registry.process_registry.kill_process",
        return_value={"status": "killed"},
    ):
        _, missing_event = controller.after_call(
            "process",
            {"action": "poll", "session_id": "proc_preview"},
            json.dumps({
                "status": "running",
                "output_preview": "Error: supabaseUrl is required.",
            }),
            session_cwd=str(root),
            visual_required=True,
        )
    assert missing_event["failure_class"] == "missing_environment"
    assert "Required visual QA remains pending" in missing_event["summary"]

    third_local = controller.before_call(
        "terminal",
        {
            "command": "pnpm --dir dashboard qa:with-env -- pnpm preview --port 5174",
            "background": True,
            "workdir": str(root),
        },
        session_cwd=str(root),
        visual_required=True,
    )
    assert third_local.code == "preview_local_budget_exhausted"

    assert (
        controller.before_call(
            "terminal",
            {"command": "pnpm --dir dashboard qa:auth", "workdir": str(root)},
            session_cwd=str(root),
            visual_required=True,
        )
        is None
    )


def test_later_turn_reset_reopens_same_launcher_for_corrected_environment(tmp_path):
    root = _frontend_repo(tmp_path)
    controller = PreviewReadinessController()
    _start(controller, root)
    with patch(
        "tools.process_registry.process_registry.kill_process",
        return_value={"status": "killed"},
    ):
        controller.after_call(
            "process",
            {"action": "poll", "session_id": "proc_dev"},
            json.dumps({"status": "running", "output": "supabaseUrl is required"}),
            session_cwd=str(root),
            visual_required=False,
        )

    same_args = {
        "command": "pnpm --dir dashboard dev --host 127.0.0.1 --port 5174",
        "background": True,
        "workdir": str(root),
    }
    assert (
        controller.before_call(
            "terminal", same_args, session_cwd=str(root), visual_required=False
        ).code
        == "preview_equivalent_retry_blocked"
    )

    controller.reset_for_turn()
    assert (
        controller.before_call(
            "terminal", same_args, session_cwd=str(root), visual_required=False
        )
        is None
    )


def test_transient_browser_failure_does_not_trip_deterministic_circuit(tmp_path):
    root = _frontend_repo(tmp_path)
    controller = PreviewReadinessController()
    _start(controller, root)
    _, event = controller.after_call(
        "browser_navigate",
        {"url": "http://127.0.0.1:5174/races/239"},
        json.dumps({"success": False, "error": "net::ERR_CONNECTION_REFUSED"}),
        session_cwd=str(root),
        visual_required=True,
    )
    assert event["status"] == "unavailable"
    assert event["deterministic"] is False
    assert (
        controller.before_call(
            "browser_navigate",
            {"url": "http://127.0.0.1:5174/races/239"},
            session_cwd=str(root),
            visual_required=True,
        )
        is None
    )


def test_browser_internal_error_cleanup_refines_missing_environment_evidence(tmp_path):
    root = _frontend_repo(tmp_path)
    controller = PreviewReadinessController()
    _start(controller, root)
    with patch(
        "tools.process_registry.process_registry.kill_process",
        return_value={
            "status": "killed",
            "output": "[500] GET /races/239\nError: supabaseUrl is required.",
        },
    ):
        _, event = controller.after_call(
            "browser_navigate",
            {"url": "http://127.0.0.1:5174/races/239"},
            json.dumps({
                "success": True,
                "title": "Internal Error",
                "snapshot": 'heading "Internal Error"',
            }),
            session_cwd=str(root),
            visual_required=True,
        )
    assert event["failure_class"] == "missing_environment"
    assert event["cleanup"]["status"] == "killed"


def test_preview_runtime_evidence_is_bounded_and_cannot_pass_visual_qa():
    stats = {
        "visual_qa_level": "surface",
        "visual_qa_receipts": [],
        "preview_readiness_events": [],
    }
    for index in range(MAX_PREVIEW_EVENTS + 5):
        record_preview_event(
            stats,
            {
                "status": "failed",
                "deterministic": True,
                "failure_class": "missing_environment",
                "strategy": f"local_preview_{index}",
                "strategy_hash": f"{index + 1:012x}",
                "summary": "x" * 500,
                "recommended_launcher": "pnpm --dir dashboard qa:auth",
            },
        )

    summary = summarize_preview_events(stats["preview_readiness_events"])
    breakdown = build_turn_runtime_breakdown(stats)

    assert len(summary["events"]) == MAX_PREVIEW_EVENTS
    assert all(len(event.get("summary", "")) <= 240 for event in summary["events"])
    assert breakdown["preview_readiness"] == summary
    assert breakdown["visual_qa"]["receipt_status"] == "missing"
    assert breakdown["visual_qa_receipts"] == []

    durable = _durable_runtime_breakdown(
        {
            **breakdown,
            "preview_readiness": {
                "events": [
                    *breakdown["preview_readiness"]["events"],
                    {
                        "status": "failed",
                        "deterministic": True,
                        "failure_class": "missing_environment",
                        "summary": "PASSWORD=ledger-secret /home/private/worktree",
                        "raw_output": "must not persist",
                    },
                ]
            },
        },
        {"level": "surface", "target": "route", "assertions": ["renders"]},
        receipt_limit=1,
    )
    rendered = json.dumps(durable["preview_readiness"])
    assert len(durable["preview_readiness"]["events"]) == MAX_PREVIEW_EVENTS
    assert "ledger-secret" not in rendered
    assert "/home/private" not in rendered
    assert "raw_output" not in rendered
