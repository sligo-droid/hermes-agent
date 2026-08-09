from agent.visual_qa import (
    build_visual_qa_response_nudge,
    build_visual_qa_followup_nudge,
    classify_visual_requirement,
    normalize_visual_qa_config,
    normalize_visual_requirement,
    promote_visual_requirement_for_mutations,
    sanitize_visual_receipt,
    visual_receipt_completion,
    visual_requirement_id,
    visual_requirement_uses_orchestrator_contract,
)


def _receipt(requirement, *, status="passed", order=4):
    normalized = normalize_visual_requirement(requirement)
    assertion_ids = [
        item["id"] for item in normalized["assertions"] if isinstance(item, dict)
    ]
    receipt = {
        "requirement_id": visual_requirement_id(normalized),
        "contract_id": "vac_" + ("a" * 24),
        "assertion_ids": assertion_ids,
        "status": status,
        "attempts": 1,
        "vision_calls": 0,
        "duration_ms": 25,
        "diagnostic_codes": ["viewport_contained_satisfied"],
        "order": order,
    }
    if visual_requirement_uses_orchestrator_contract(normalized):
        receipt["coverage_ids"] = assertion_ids
        receipt["assertion_ids"] = ["vassert_" + ("c" * 24)]
    return receipt


def test_classifier_recognizes_explicit_artifact_and_surface_work():
    artifact = classify_visual_requirement("Add a Download PNG export for the national map.")
    surface = classify_visual_requirement("Make the mobile toolbar layout responsive without horizontal overflow.")

    assert artifact["level"] == "artifact"
    assert artifact["assertions"]
    assert artifact["assertions"][0]["kind"] == "orchestrator_contract"
    assert surface["level"] == "surface"
    assert surface["assertions"]
    assert surface["assertions"][0]["kind"] == "orchestrator_contract"


def test_visual_receipt_preserves_two_bounded_vision_calls():
    requirement = classify_visual_requirement(
        "Build a responsive dashboard with a mobile sidebar.",
        worker_route="action",
    )
    receipt = _receipt(requirement, status="uncertain")
    receipt["vision_calls"] = 2

    sanitized = sanitize_visual_receipt(receipt, requirement=requirement)

    assert sanitized is not None
    assert sanitized["vision_calls"] == 2


def test_classifier_excludes_review_only_work():
    assert classify_visual_requirement("Review this screenshot and explain the current layout.")["level"] == "none"
    assert classify_visual_requirement("Document the PNG export format.")["level"] == "none"
    assert classify_visual_requirement("Update the responsive layout documentation.")["level"] == "none"


def test_classifier_excludes_plan_only_visual_work():
    request = (
        "The search bar at the top of the page is jarring because it breaks up "
        "the flow of the layout. Please create a plan for making this improvement."
    )

    assert classify_visual_requirement(request, worker_route="action")["level"] == "none"
    assert (
        classify_visual_requirement(
            "Create a plan to fix the dashboard search layout.",
            worker_route="action",
        )["level"]
        == "none"
    )


def test_classifier_keeps_plan_plus_implementation_visual_work():
    requirement = classify_visual_requirement(
        "Create a plan for the dashboard search bar and implement it.",
        worker_route="action",
    )

    assert requirement["level"] == "surface"


def test_classifier_recognizes_explicit_visual_qa_check_without_code_action():
    requirement = classify_visual_requirement(
        "Confirm you can use the visual QA tool successfully now. Mimic the closeout flow.",
        worker_route={"runtime_mode": "read_only"},
    )

    assert requirement["level"] == "surface"
    assert requirement["assertions"][0]["kind"] == "orchestrator_contract"


def test_classifier_allows_explicit_visual_fix_after_review_framing():
    requirement = classify_visual_requirement("Review the dashboard and fix mobile overflow in the toolbar.")

    assert requirement["level"] == "surface"
    assert requirement["assertions"]


def test_classifier_recognizes_direct_visual_defects_and_desired_state_requests():
    incident = classify_visual_requirement(
        "in the Issue Attention graph in the State Brief page:\n"
        "-the bar graphs clip through the x axis\n"
        "-we should lightly label the y axis",
        worker_route="action",
    )
    defect_only = classify_visual_requirement(
        "The dashboard chart bars clip through the x axis.",
        worker_route="action",
    )
    desired_state = classify_visual_requirement("The dashboard should lightly label the y axis.")

    assert incident["level"] == "surface"
    assert incident["assertions"]
    assert incident["assertions"][0]["kind"] == "orchestrator_contract"
    assert defect_only["level"] == "surface"
    assert desired_state["level"] == "surface"


def test_classifier_recognizes_plural_map_implementation_incident():
    requirement = classify_visual_requirement(
        "Let's repair this in the local district maps. Also, let's choose a "
        "non-blue, non-red color scheme.",
        worker_route="action",
    )

    assert requirement["level"] == "surface"
    assert requirement["assertions"][0]["kind"] == "orchestrator_contract"


def test_rendered_mutation_fallback_promotes_only_action_turns():
    promoted = promote_visual_requirement_for_mutations(
        {"level": "none"},
        ["dashboard/src/lib/StateDistrictMap.svelte"],
        actionable=True,
    )

    assert promoted["level"] == "surface"
    assert promoted["assertions"][0]["kind"] == "orchestrator_contract"
    assert promote_visual_requirement_for_mutations(
        {"level": "none"},
        ["dashboard/src/lib/StateDistrictMap.svelte"],
        actionable=False,
    )["level"] == "none"
    assert promote_visual_requirement_for_mutations(
        {"level": "none"},
        ["dashboard/src/lib/StateDistrictMap.test.ts", "docs/maps.md"],
        actionable=True,
    )["level"] == "none"


def test_classifier_keeps_visual_defect_review_only_requests_non_actionable():
    requirement = classify_visual_requirement(
        "Review this screenshot where the dashboard chart clips and explain why.",
        worker_route="read_only",
    )

    assert requirement["level"] == "none"


def test_classifier_drops_credential_bearing_request_text_from_requirement():
    requirement = classify_visual_requirement(
        "Add a PNG export at https://example.test/export?access_token=super-secret-token "
        "and keep the attribution inside image bounds."
    )

    assert requirement["level"] == "artifact"
    serialized = repr(requirement).lower()
    assert "http" not in serialized
    assert "token" not in serialized
    assert "super-secret" not in serialized


def test_durable_visual_requirement_uses_only_opaque_content_ids():
    protected = [
        "internal.example.test/admin",
        "/private/worktree/src/dashboard.tsx",
        "[data-secret='account-panel']",
        "api_key=not-a-real-key",
        "Welcome Alice, balance 123.45",
    ]
    requirement = {
        "level": "surface",
        "target": protected[0],
        "assertions": protected[1:],
    }

    normalized = normalize_visual_requirement(requirement)
    serialized = repr(normalized)

    assert normalized["target"].startswith("vtarget_")
    assert all(
        item["id"].startswith("vassert_")
        and item["kind"] in {"no_horizontal_overflow", "viewport_contained", "screenshot_appearance"}
        for item in normalized["assertions"]
    )
    for value in protected:
        assert value not in serialized
    assert normalize_visual_requirement(normalized) == normalized


def test_receipt_requires_safe_assertion_driven_metadata():
    requirement = {
        "level": "artifact",
        "target": "national-map-export",
        "assertions": ["attribution remains inside image bounds"],
    }
    receipt = sanitize_visual_receipt(_receipt(requirement), requirement)

    assert receipt is not None
    assert receipt["status"] == "passed"
    assert set(receipt) == {
        "requirement_id",
        "contract_id",
        "assertion_ids",
        "status",
        "attempts",
        "vision_calls",
        "duration_ms",
        "diagnostic_codes",
        "order",
    }
    assert "secret" not in repr(receipt)
    assert sanitize_visual_receipt(
        {**receipt, "evidence_ref": "cookie=session-value"},
        requirement,
    ) is None
    assert sanitize_visual_receipt(
        {**receipt, "requirement_id": "vrq_" + ("b" * 24)},
        requirement,
    ) is None
    assert sanitize_visual_receipt(
        {**receipt, "diagnostic_codes": ["model_authored_success"]},
        requirement,
    ) is None


def test_orchestrated_receipt_requires_exact_host_coverage_binding():
    requirement = classify_visual_requirement(
        "Fix the Issue Attention chart on the State Brief page.",
        worker_route="action",
    )
    receipt = _receipt(requirement)

    assert sanitize_visual_receipt(receipt, requirement) is not None
    without_coverage = dict(receipt)
    without_coverage.pop("coverage_ids")
    assert sanitize_visual_receipt(without_coverage, requirement) is None
    assert sanitize_visual_receipt(
        {**receipt, "coverage_ids": ["vassert_" + ("d" * 24)]},
        requirement,
    ) is None


def test_receipt_completion_uses_latest_fresh_matching_receipt():
    requirement = {
        "level": "surface",
        "target": "mobile-toolbar",
        "assertions": ["toolbar has no horizontal overflow"],
    }
    failed = _receipt(requirement, status="failed", order=2)
    passed = _receipt(requirement, status="passed", order=4)

    assert visual_receipt_completion(requirement, [failed, passed], min_order=3)["status"] == "passed"
    assert visual_receipt_completion(requirement, [failed, passed], min_order=5)["status"] == "missing"


def test_visual_followup_is_single_and_requires_code_change():
    requirement = {
        "level": "surface",
        "target": "mobile-toolbar",
        "assertions": ["toolbar has no horizontal overflow"],
    }

    nudge = build_visual_qa_followup_nudge(requirement, ["src/toolbar.tsx"], [], attempts=0)

    assert nudge is not None
    assert "generic screenshot" in nudge
    assert build_visual_qa_followup_nudge(requirement, ["src/toolbar.tsx"], [], attempts=1) is None
    assert build_visual_qa_followup_nudge(requirement, [], [], attempts=0) is None


def test_classifier_uses_one_opaque_orchestrator_coverage_anchor():
    requirement = classify_visual_requirement(
        "Implement the responsive toolbar.\n"
        "Keep the toolbar inside the mobile viewport without horizontal overflow."
    )

    assert requirement["level"] == "surface"
    assert len(requirement["assertions"]) == 1
    assert requirement["assertions"][0]["id"].startswith("vassert_")
    assert requirement["assertions"][0]["kind"] == "orchestrator_contract"
    assert visual_requirement_uses_orchestrator_contract(requirement) is True
    assert "responsive toolbar" not in repr(requirement).lower()
    assert "mobile viewport" not in repr(requirement).lower()


def test_uncertain_is_a_safe_non_passing_receipt_status():
    requirement = {
        "level": "surface",
        "target": "mobile-toolbar",
        "assertions": ["toolbar has no horizontal overflow"],
    }
    receipt = sanitize_visual_receipt(
        _receipt(requirement, status="uncertain", order=2),
        requirement,
    )

    assert receipt is not None
    assert visual_receipt_completion(requirement, [receipt], min_order=2)["status"] == "uncertain"


def test_visual_followup_directs_model_to_dedicated_tool():
    requirement = {
        "level": "surface",
        "target": "mobile-toolbar",
        "assertions": ["toolbar has no horizontal overflow"],
    }

    nudge = build_visual_qa_followup_nudge(requirement, ["src/toolbar.tsx"], [], attempts=0)

    assert nudge is not None
    assert "call the `visual_qa` tool" in nudge
    assert "attach `visual_qa_receipt`" not in nudge


def test_visual_response_nudge_uses_trusted_passed_state_not_output_phrase():
    requirement = classify_visual_requirement(
        "Split the confidence charts and add component breakdowns.",
        worker_route="action",
    )
    receipt = _receipt(requirement, order=8)

    nudge = build_visual_qa_response_nudge(
        requirement=requirement,
        receipts=[receipt],
        original_request="Split the confidence charts and add component breakdowns.",
        platform="discord",
        runtime_mode="action",
        config={"mode": "enforce_explicit"},
        changed_paths=["dashboard/src/routes/confidence/+page.svelte"],
        last_edit_order=7,
    )

    assert nudge is not None
    assert "one complete response to the original request" in nudge
    assert "what changed" in nudge
    assert "PR, merge, deploy, or live state" in nudge
    assert "Do not call more tools" in nudge


def test_visual_response_nudge_always_synthesizes_once_after_passed_state():
    requirement = classify_visual_requirement(
        "Split the confidence charts and add component breakdowns.",
        worker_route="action",
    )
    receipt = _receipt(requirement, order=8)
    common = {
        "requirement": requirement,
        "receipts": [receipt],
        "original_request": "Split the confidence charts and add component breakdowns.",
        "platform": "discord",
        "runtime_mode": "action",
        "config": {"mode": "enforce_explicit"},
        "changed_paths": ["dashboard/src/routes/confidence/+page.svelte"],
        "last_edit_order": 7,
    }

    assert build_visual_qa_response_nudge(**common) is not None
    assert build_visual_qa_response_nudge(attempts=1, **common) is None


def test_visual_response_nudge_respects_explicit_qa_only_requests():
    requirement = classify_visual_requirement(
        "Make the dashboard responsive.",
        worker_route="action",
    )

    assert build_visual_qa_response_nudge(
        requirement=requirement,
        receipts=[_receipt(requirement, order=8)],
        original_request="Only tell me whether visual QA passed or failed.",
        platform="discord",
        runtime_mode="action",
        config={"mode": "enforce_explicit"},
        changed_paths=["dashboard/src/app.tsx"],
        last_edit_order=7,
    ) is None

    for request in (
        "Only report the visual QA result.",
        "Just give me the visual QA result.",
        "Report only whether it passed visual QA.",
        "Please respond only with whether visual QA passed.",
        "Tell me only whether visual QA passed.",
        "Only say whether visual QA passed.",
        "Answer only whether visual QA passed.",
        "Only tell me whether visual QA passed for this change.",
    ):
        assert build_visual_qa_response_nudge(
            requirement=requirement,
            receipts=[_receipt(requirement, order=8)],
            original_request=request,
            platform="discord",
            runtime_mode="action",
            config={"mode": "enforce_explicit"},
            changed_paths=["dashboard/src/app.tsx"],
            last_edit_order=7,
        ) is None


def test_visual_response_nudge_does_not_exempt_ordinary_only_language():
    requirement = classify_visual_requirement(
        "Only change the dashboard colors and run visual QA.",
        worker_route="action",
    )

    assert build_visual_qa_response_nudge(
        requirement=requirement,
        receipts=[_receipt(requirement, order=8)],
        original_request="Only change the dashboard colors and run visual QA.",
        platform="discord",
        runtime_mode="action",
        config={"mode": "enforce_explicit"},
        changed_paths=["dashboard/src/app.tsx"],
        last_edit_order=7,
    ) is not None


def test_visual_response_nudge_does_not_exempt_wait_until_qa_language():
    requirement = classify_visual_requirement(
        "Change the dashboard colors and run visual QA.",
        worker_route="action",
    )

    assert build_visual_qa_response_nudge(
        requirement=requirement,
        receipts=[_receipt(requirement, order=8)],
        original_request=(
            "Only respond after visual QA passes, and include a full summary of what changed."
        ),
        platform="discord",
        runtime_mode="action",
        config={"mode": "enforce_explicit"},
        changed_paths=["dashboard/src/app.tsx"],
        last_edit_order=7,
    ) is not None


def test_visual_response_nudge_does_not_exempt_qa_status_plus_summary_requests():
    requirement = classify_visual_requirement(
        "Change the dashboard colors and run visual QA.",
        worker_route="action",
    )

    for request in (
        "Only report if visual QA passed, and include a full summary of what changed.",
        "Just tell me if visual QA passed before you send the complete implementation summary.",
        "Only tell me whether visual QA passed and list the changes you made.",
        "Only report whether visual QA passed, then describe the implementation.",
        "Just tell me if visual QA passed and include the files changed.",
        "Only answer whether visual QA passed and provide the delivery status.",
        "Only tell me whether visual QA passed and explain what you changed.",
        "Just say if visual QA passed, along with the changes you made.",
        "Only report whether visual QA passed and give me the implementation details.",
        "Only tell me whether visual QA passed and what you did.",
        "Only tell me whether visual QA passed and which files you changed.",
        "Only tell me whether visual QA passed plus the verification results.",
        "Only tell me whether visual QA passed and whether it was deployed.",
    ):
        assert build_visual_qa_response_nudge(
            requirement=requirement,
            receipts=[_receipt(requirement, order=8)],
            original_request=request,
            platform="discord",
            runtime_mode="action",
            config={"mode": "enforce_explicit"},
            changed_paths=["dashboard/src/app.tsx"],
            last_edit_order=7,
        ) is not None


def test_config_invalid_values_fall_back_to_bounded_shadow_mode():
    assert normalize_visual_qa_config(
        {
            "mode": "enforce_everything",
            "max_receipts_per_turn": 999,
            "max_attempts": 999,
            "max_assertions": 999,
            "max_vision_calls": 999,
            "attempt_timeout_s": 999,
            "total_timeout_s": 999,
            "max_output_chars": 999_999,
        }
    ) == {
        "mode": "shadow",
        "max_receipts_per_turn": 1,
        "max_followup_turns": 1,
        "max_attempts": 2,
        "max_assertions": 6,
        "max_vision_calls": 2,
        "attempt_timeout_s": 30.0,
        "total_timeout_s": 60.0,
        "max_output_chars": 6000,
    }


def test_config_allows_lower_budgets_without_allowing_unbounded_work():
    normalized = normalize_visual_qa_config(
        {
            "mode": "enforce_explicit",
            "max_attempts": 1,
            "max_assertions": 2,
            "max_vision_calls": 0,
            "attempt_timeout_s": 4,
            "total_timeout_s": 8,
            "max_output_chars": 900,
        }
    )

    assert normalized == {
        "mode": "enforce_explicit",
        "max_receipts_per_turn": 1,
        "max_followup_turns": 1,
        "max_attempts": 1,
        "max_assertions": 2,
        "max_vision_calls": 0,
        "attempt_timeout_s": 4.0,
        "total_timeout_s": 8.0,
        "max_output_chars": 900,
    }


def test_config_upgrades_legacy_one_call_vision_budget_to_required_pair():
    assert normalize_visual_qa_config({"max_vision_calls": 1})["max_vision_calls"] == 2
