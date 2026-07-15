from agent.visual_qa import (
    build_visual_qa_followup_nudge,
    classify_visual_requirement,
    normalize_visual_qa_config,
    sanitize_visual_receipt,
    visual_receipt_completion,
)


def test_classifier_recognizes_explicit_artifact_and_surface_work():
    artifact = classify_visual_requirement("Add a Download PNG export for the national map.")
    surface = classify_visual_requirement("Make the mobile toolbar layout responsive without horizontal overflow.")

    assert artifact["level"] == "artifact"
    assert artifact["assertions"]
    assert surface["level"] == "surface"
    assert surface["assertions"]


def test_classifier_excludes_review_only_work():
    assert classify_visual_requirement("Review this screenshot and explain the current layout.")["level"] == "none"
    assert classify_visual_requirement("Document the PNG export format.")["level"] == "none"
    assert classify_visual_requirement("Update the responsive layout documentation.")["level"] == "none"


def test_classifier_allows_explicit_visual_fix_after_review_framing():
    requirement = classify_visual_requirement("Review the dashboard and fix mobile overflow in the toolbar.")

    assert requirement["level"] == "surface"
    assert requirement["assertions"]


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


def test_receipt_requires_safe_assertion_driven_metadata():
    requirement = {
        "level": "artifact",
        "target": "national-map-export",
        "assertions": ["attribution remains inside image bounds"],
    }
    receipt = sanitize_visual_receipt(
        {
            **requirement,
            "check": "rendered-artifact-inspection",
            "status": "passed",
            "evidence_ref": "representative export inspected",
            "order": 4,
        },
        requirement,
    )

    assert receipt is not None
    assert receipt["status"] == "passed"
    assert sanitize_visual_receipt({**receipt, "evidence_ref": "https://user:secret@example.test/export"}) is None
    assert sanitize_visual_receipt({**receipt, "evidence_ref": "cookie=session-value"}) is None
    assert sanitize_visual_receipt({**receipt, "evidence_ref": "x" * 241}) is None
    assert sanitize_visual_receipt({**receipt, "target": "https://example.test/?token=secret"}) is None
    assert sanitize_visual_receipt({**receipt, "check": "Bearer super-secret"}) is None


def test_receipt_completion_uses_latest_fresh_matching_receipt():
    requirement = {
        "level": "surface",
        "target": "mobile-toolbar",
        "assertions": ["toolbar has no horizontal overflow"],
    }
    failed = {
        **requirement,
        "check": "desktop-browser-inspection",
        "status": "failed",
        "evidence_ref": "desktop inspection recorded",
        "order": 2,
    }
    passed = {**failed, "status": "passed", "order": 4, "evidence_ref": "mobile inspection recorded"}

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


def test_config_invalid_values_fall_back_to_bounded_shadow_mode():
    assert normalize_visual_qa_config({"mode": "enforce_everything", "max_receipts_per_turn": 999}) == {
        "mode": "shadow",
        "max_receipts_per_turn": 1,
        "max_followup_turns": 1,
    }
