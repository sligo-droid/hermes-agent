from hermes_cli.grill_me import build_grill_me_prompt, detect_grill_me_trigger


def test_detect_grill_me_trigger_matches_clear_imperatives():
    matches = [
        "grill me on this plan",
        "can you grill me before we build this?",
        "grill   me: dashboard auth flow",
        "use grill me for this feature",
        "please GRILL me about X",
        "let's build dashboard auth; grill me first",
        "research the codebase, then grill me on the plan",
    ]

    for text in matches:
        assert detect_grill_me_trigger(text), text


def test_detect_grill_me_trigger_rejects_meta_negation_and_slash():
    rejects = [
        "research the grill me feature",
        "plan a feature called grill me",
        "build a command named grill me",
        "what does grill me mean?",
        "don't grill me",
        "no need to grill me",
        "/grill me on this plan",
    ]

    for text in rejects:
        assert not detect_grill_me_trigger(text), text


def test_build_grill_me_prompt_is_planning_only_and_preserves_request():
    prompt = build_grill_me_prompt("grill me: dashboard auth flow", runtime_note="Discord thread")

    assert "planning mode is active" in prompt
    assert "Do not implement code" in prompt
    assert "AGENTS.md" in prompt
    assert "CONTEXT.md" in prompt
    assert "CONTEXT-MAP.md" in prompt
    assert "docs/adr/" in prompt
    assert "propose precise canonical language" in prompt
    assert "inspect that context instead of asking the user" in prompt
    assert "Do not edit docs in grill-me planning mode" in prompt
    assert "after explicit confirmation" in prompt
    assert "Do not batch dependent questions" in prompt
    assert "grill me: dashboard auth flow" in prompt
    assert "Discord thread" in prompt
