from agent.request_text import flatten_request_for_matching, merge_request_fragments


def test_merge_request_fragments_deduplicates_without_flattening_first_copy():
    first = "Implement the toolbar.\nKeep it within the mobile viewport."
    duplicate = "  Implement   the toolbar. Keep it within the mobile viewport.  "

    merged = merge_request_fragments(first, duplicate, "", None)

    assert merged == first
    assert "\n" in merged


def test_merge_request_fragments_preserves_distinct_sentence_boundaries():
    merged = merge_request_fragments(
        "Implement the toolbar.",
        "Keep it within the mobile viewport.\nDo not add horizontal overflow.",
    )

    assert merged == (
        "Implement the toolbar.\n\n"
        "Keep it within the mobile viewport.\nDo not add horizontal overflow."
    )


def test_merge_request_fragments_has_a_hard_total_bound():
    merged = merge_request_fragments("a" * 20, "b" * 20, max_chars=25)

    assert len(merged) <= 25
    assert merged.startswith("a" * 20)


def test_flatten_request_for_matching_collapses_whitespace_only_for_matching():
    assert flatten_request_for_matching("Implement UI.\n\n  Keep   spacing.") == (
        "Implement UI. Keep spacing."
    )
