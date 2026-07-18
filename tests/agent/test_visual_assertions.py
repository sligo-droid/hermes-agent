from agent.visual_assertions import aggregate_assertion_results, validate_visual_assertions


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
