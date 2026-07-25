from agent.visual_assertions import (
    aggregate_assertion_results,
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


def test_orchestrated_contract_preserves_rich_semantics_only_transiently():
    raw = _incident_contract()

    normalized = normalize_orchestrated_visual_contract(raw)
    safe = storage_safe_visual_qa_args(raw)

    assert normalized["target"]["description"] == "Issue Attention graph region"
    assert normalized["page"]["state"] == "prepared"
    assert normalized["viewport"]["width"] == 1440
    assert normalized["state"] == ["chart data loaded", "bars and both axes visible"]
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
