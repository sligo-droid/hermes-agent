from agent.visual_assertions import (
    aggregate_assertion_results,
    diagnose_orchestrated_visual_contract,
    is_storage_safe_visual_qa_args,
    normalize_assertion_result_coverage,
    normalize_orchestrated_visual_contract,
    storage_safe_visual_qa_args,
    validate_visual_assertions,
    visual_execution_contract_id,
)


def _incident_contract():
    return {
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


def test_storage_safe_visual_qa_shape_is_recognized_but_not_executable():
    safe = storage_safe_visual_qa_args(_incident_contract())

    assert is_storage_safe_visual_qa_args(safe) is True
    assert normalize_orchestrated_visual_contract(safe) == {}
    assert is_storage_safe_visual_qa_args({"assertions": []}) is True


def test_storage_safe_visual_qa_shape_rejects_lookalikes():
    safe = storage_safe_visual_qa_args(_incident_contract())

    assert is_storage_safe_visual_qa_args({**safe, "target": {}}) is False
    assert is_storage_safe_visual_qa_args(
        {"assertions": [{"id": "vassert_" + "a" * 24, "kind": "visible", "locator": {}}]}
    ) is False
    assert is_storage_safe_visual_qa_args(
        {"contract_id": "vac_not-valid", "assertions": safe["assertions"]}
    ) is False


def test_every_legacy_storage_producer_shape_is_recognized():
    duplicate_assertion = {
        "id": "vassert_" + "a" * 24,
        "kind": "screenshot_appearance",
    }
    safe = storage_safe_visual_qa_args(
        {"assertions": [duplicate_assertion, duplicate_assertion]}
    )

    assert safe == {"assertions": [duplicate_assertion, duplicate_assertion]}
    assert is_storage_safe_visual_qa_args(safe) is True
    assert normalize_orchestrated_visual_contract(safe) == {}


def test_validation_accepts_bounded_declarative_assertions():
    assertions = validate_visual_assertions(
        [
            {
                "id": "toolbar-visible",
                "kind": "visible",
                "locator": {"by": "test_id", "value": "mobile-toolbar"},
            },
            {
                "id": "toolbar-contained",
                "kind": "viewport_contained",
                "locator": {"by": "role", "value": "toolbar", "name": "Main"},
            },
            {
                "id": "no-console-errors",
                "kind": "no_new_diagnostics",
                "cursor": "dcur_4_0123456789abcdef01234567",
            },
        ]
    )

    assert [item["id"] for item in assertions] == [
        "toolbar-visible",
        "toolbar-contained",
        "no-console-errors",
    ]


def test_validation_rejects_model_authored_execution_surfaces():
    assertions = validate_visual_assertions(
        [
            {"id": "bad-js", "kind": "javascript", "expression": "document.body"},
            {"id": "bad-shell", "kind": "shell", "command": "curl example.test"},
            {
                "id": "bad-css",
                "kind": "visible",
                "locator": {"by": "css", "value": "div; fetch('/secret')"},
            },
        ]
    )

    assert assertions == []


def test_validation_bounds_assertion_count_and_deduplicates_ids():
    raw = [
        {
            "id": f"a-{index if index < 6 else 0}",
            "kind": "exists",
            "locator": {"by": "test_id", "value": f"node-{index}"},
        }
        for index in range(8)
    ]

    assertions = validate_visual_assertions(raw, max_assertions=6)

    assert len(assertions) == 6
    assert len({item["id"] for item in assertions}) == 6


def test_aggregate_fails_closed_for_every_non_pass_status():
    assert aggregate_assertion_results([{"id": "a", "status": "passed", "code": "exists_satisfied"}])["status"] == "passed"
    assert aggregate_assertion_results([{"id": "a", "status": "uncertain", "code": "attempt_timeout"}])["status"] == "uncertain"
    assert aggregate_assertion_results([{"id": "a", "status": "blocked", "code": "element_lookup_unavailable"}])["status"] == "blocked"
    assert aggregate_assertion_results([{"id": "a", "status": "failed", "code": "exists_mismatch"}])["status"] == "failed"
    assert aggregate_assertion_results([{"id": "a", "status": "passed", "code": "model_prose"}])["status"] == "uncertain"
    assert aggregate_assertion_results([])["status"] == "uncertain"


def test_exact_result_coverage_rejects_missing_duplicate_extra_and_malformed_results():
    expected = ["a", "b"]
    valid = [
        {"id": "b", "status": "passed", "code": "visible_satisfied"},
        {"id": "a", "status": "failed", "code": "exists_mismatch"},
    ]
    normalized = normalize_assertion_result_coverage(valid, expected)
    assert normalized["valid"] is True
    assert [item["id"] for item in normalized["results"]] == expected

    invalid_values = [
        valid[:1],
        [valid[0], valid[0]],
        [valid[0], {"id": "extra", "status": "passed", "code": "exists_satisfied"}],
        [valid[0], {"id": "a", "status": "passed", "code": "model_prose"}],
        [valid[0], "malformed"],
    ]
    for value in invalid_values:
        result = normalize_assertion_result_coverage(value, expected)
        assert result == {
            "valid": False,
            "results": [
                {"id": "a", "status": "uncertain", "code": "invalid_assertion_results"},
                {"id": "b", "status": "uncertain", "code": "invalid_assertion_results"},
            ],
        }


def test_invalid_contract_diagnostics_are_fixed_and_do_not_echo_input():
    result = diagnose_orchestrated_visual_contract(
        {"target": {"description": "PRIVATE TARGET"}, "unexpected": "SECRET VALUE"}
    )

    assert result["contract"] == {}
    assert result["reason_code"] == "contract_unknown_fields"
    assert result["correction"] == (
        "Use only target, page, viewport, state, artifacts, and assertions."
    )
    assert "PRIVATE" not in repr(result)
    assert "SECRET" not in repr(result)


def test_orchestrated_contract_preserves_rich_semantics_only_transiently():
    raw = _incident_contract()

    normalized = normalize_orchestrated_visual_contract(raw)
    safe = storage_safe_visual_qa_args(raw)

    assert normalized["target"]["description"] == "Issue Attention graph region"
    assert normalized["page"]["state"] == "prepared"
    assert normalized["viewport"]["width"] == 1440
    assert normalized["state"] == ["chart data loaded", "bars and both axes visible"]
    assert normalized["artifacts"] == [
        {
            "kind": "focused",
            "description": "Issue Attention graph region",
            "locator": {"by": "test_id", "value": "issue-attention-graph"},
            "viewport": raw["viewport"],
        },
        {
            "kind": "context",
            "description": "Surrounding page context",
            "viewport": raw["viewport"],
        },
    ]
    assert len(normalized["assertions"]) == 2
    assert all(item["id"].startswith("vassert_") for item in normalized["assertions"])
    assert safe == {
        "contract_id": visual_execution_contract_id(normalized),
        "assertions": [
            {"id": item["id"], "kind": "screenshot_appearance"}
            for item in normalized["assertions"]
        ],
    }
    serialized = repr(safe)
    assert "Issue Attention" not in serialized
    assert "x-axis" not in serialized
    assert "y-axis" not in serialized.lower()
    assert "issue-attention-graph" not in serialized


def test_orchestrated_contract_normalizes_provider_limit_overages():
    raw = _incident_contract()
    raw["assertions"][0]["expectation"] = "balanced layout " * 30
    raw["assertions"].extend(
        {
            "kind": "text_present",
            "text": f"requested label {index}",
        }
        for index in range(6)
    )

    diagnosed = diagnose_orchestrated_visual_contract(raw, max_assertions=6)

    assert diagnosed["reason_code"] == ""
    assert len(diagnosed["contract"]["assertions"]) == 6
    assert diagnosed["contract"]["assertions"][0]["kind"] == "screenshot_appearance"
    assert 200 <= len(diagnosed["contract"]["assertions"][0]["expectation"]) <= 240


def test_orchestrated_contract_omits_diagnostics_without_host_cursor():
    raw = _incident_contract()
    raw["assertions"].append({"kind": "no_new_diagnostics"})

    normalized = normalize_orchestrated_visual_contract(raw)

    assert [item["kind"] for item in normalized["assertions"]] == [
        "screenshot_appearance",
        "screenshot_appearance",
    ]


def test_orchestrated_contract_deduplicates_and_bounds_screenshot_artifacts():
    raw = _incident_contract()
    raw["artifacts"] = [
        {
            "kind": "focused",
            "description": "Changed chart",
            "locator": {"by": "test_id", "value": "issue-attention-graph"},
        },
        {
            "kind": "focused",
            "description": "Duplicate capture",
            "locator": {"by": "test_id", "value": "issue-attention-graph"},
        },
        {
            "kind": "context",
            "description": "State Brief context",
        },
        {
            "kind": "responsive",
            "description": "Narrow chart layout",
            "locator": {"by": "test_id", "value": "issue-attention-graph"},
            "viewport": {
                "description": "narrow responsive viewport",
                "width": 390,
                "height": 844,
            },
        },
    ]

    normalized = normalize_orchestrated_visual_contract(raw)

    assert [item["kind"] for item in normalized["artifacts"]] == [
        "focused",
        "context",
        "responsive",
    ]
    assert normalized["artifacts"][2]["viewport"]["width"] == 390

    raw["artifacts"].append(
        {"kind": "context", "description": "A fifth requested screenshot"}
    )
    assert normalize_orchestrated_visual_contract(raw) == {}


def test_orchestrated_contract_accepts_safe_schema_permitted_model_shapes():
    raw = _incident_contract()
    raw["artifacts"] = [
        {
            "kind": "context",
            "description": "Updates row context",
            "locator": {"by": "css", "value": ".updates-grid"},
        },
        {
            "kind": "focused",
            "description": "Current page visual",
        },
    ]
    raw["assertions"] = [
        {
            "kind": "text_present",
            "locator": {"by": "css", "value": ".update-panel--polls"},
            "text": "Recent Polls",
        },
        {
            "kind": "screenshot_appearance",
            "locator": {"by": "css", "value": ".update-panel--polls"},
            "policy": "literal_request_text",
            "expectation": "The recent polls panel matches the adjacent panel height.",
        },
    ]

    normalized = normalize_orchestrated_visual_contract(raw)

    assert [item["kind"] for item in normalized["artifacts"]] == [
        "context",
        "focused",
    ]
    assert normalized["assertions"][0]["kind"] == "text_present"
    assert normalized["assertions"][0]["policy"] == "literal_request_text"
    assert "locator" not in normalized["assertions"][0]
    assert normalized["assertions"][1]["kind"] == "screenshot_appearance"
    assert "locator" not in normalized["assertions"][1]
    assert "policy" not in normalized["assertions"][1]


def test_responsive_artifact_requires_bounded_dimensions():
    raw = _incident_contract()
    raw["artifacts"] = [
        {
            "kind": "responsive",
            "description": "Narrow chart layout",
            "viewport": {"description": "narrow responsive viewport"},
        }
    ]

    assert normalize_orchestrated_visual_contract(raw) == {}


def test_orchestrated_contract_requires_visual_judgement_and_rejects_protected_surfaces():
    no_appearance = _incident_contract()
    no_appearance["assertions"] = [
        {
            "kind": "visible",
            "locator": {"by": "test_id", "value": "issue-attention-graph"},
        }
    ]
    unsafe_url = _incident_contract()
    unsafe_url["page"]["description"] = "https://internal.example.test/state-brief"
    unsafe_execution = _incident_contract()
    unsafe_execution["assertions"][0]["javascript"] = "document.body"

    assert normalize_orchestrated_visual_contract(no_appearance) == {}
    assert normalize_orchestrated_visual_contract(unsafe_url) == {}
    assert normalize_orchestrated_visual_contract(unsafe_execution) == {}
    assert storage_safe_visual_qa_args(unsafe_execution) == {"assertions": []}
