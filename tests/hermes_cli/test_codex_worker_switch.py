from __future__ import annotations

from hermes_cli import codex_worker_switch as cws


def test_parse_args():
    assert cws.parse_args("") == (None, [])
    assert cws.parse_args("status") == (None, [])
    assert cws.parse_args("on") == (True, [])
    assert cws.parse_args("off") == (False, [])

    value, errors = cws.parse_args("maybe")
    assert value is None
    assert errors and "Unknown codex-worker" in errors[0]


def test_default_enabled_when_unset():
    assert cws.get_enabled({}) is True
    assert cws.get_enabled({"codex_worker": {}}) is True
    assert cws.get_enabled(None) is True  # type: ignore[arg-type]


def test_apply_persists_toggle():
    cfg = {}
    persisted = {}

    def persist(config):
        persisted.update(config)

    result = cws.apply(cfg, False, persist_callback=persist)

    assert result.success is True
    assert result.enabled is False
    assert result.old_enabled is True
    assert cfg["codex_worker"]["enabled"] is False
    assert persisted["codex_worker"]["enabled"] is False


def test_coding_request_detection_is_conservative():
    assert cws.looks_like_coding_request("implement the parser fix in src/parser.py")
    assert cws.looks_like_coding_request("use codex worker to debug failing tests")
    assert cws.looks_like_coding_request("review this diff before merge")
    assert not cws.looks_like_coding_request("what is the weather today?")


def test_codex_worker_meta_questions_do_not_trigger_guidance():
    assert not cws.looks_like_coding_request(
        'we just set "openai_runtime: auto" with codex-worker; why are responses slower?'
    )
    assert not cws.looks_like_coding_request(
        "can you run tests comparing the codex app server to opencode?"
    )
    assert not cws.looks_like_coding_request(
        "tighten the heuristic for invoking codex/opencode later"
    )


def test_echoed_codex_worker_guidance_is_ignored_before_classification():
    prefix = cws.build_worker_guidance(
        "fix tests in tests/test_parser.py",
        enabled=True,
        tool_available=True,
        api_mode="chat_completions",
    )

    assert not cws.looks_like_coding_request(prefix)
    assert not cws.looks_like_coding_request(
        prefix + "what is responsible for the performance difference?"
    )
    assert cws.looks_like_coding_request(prefix + "implement the fix in src/parser.py")


def test_worker_guidance_requires_enabled_tool_and_normal_runtime():
    msg = "fix tests in tests/test_parser.py"

    assert cws.build_worker_guidance(
        msg,
        enabled=True,
        tool_available=True,
        api_mode="chat_completions",
    )
    assert cws.build_worker_guidance(
        msg,
        enabled=False,
        tool_available=True,
        api_mode="chat_completions",
    ) == ""
    assert cws.build_worker_guidance(
        msg,
        enabled=True,
        tool_available=False,
        api_mode="chat_completions",
    ) == ""
    assert cws.build_worker_guidance(
        msg,
        enabled=True,
        tool_available=True,
        api_mode="codex_app_server",
    ) == ""
