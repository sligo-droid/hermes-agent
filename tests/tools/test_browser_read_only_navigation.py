import json

from tools import browser_tool


def test_read_only_navigation_policy_blocks_localhost_before_local_backend(monkeypatch):
    monkeypatch.setattr(browser_tool, "_is_local_backend", lambda: True)
    monkeypatch.setattr(browser_tool, "_is_always_blocked_url", lambda _url: False)
    monkeypatch.setattr(browser_tool, "_is_safe_url", lambda url: "localhost" not in url)
    monkeypatch.setattr(browser_tool, "_allow_private_urls", lambda: False)
    monkeypatch.setattr(
        browser_tool,
        "_run_browser_command",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("browser command must not run")
        ),
    )

    payload = json.loads(
        browser_tool.browser_navigate(
            "http://localhost:8000/admin",
            task_id="task-1::read-only",
            read_only=True,
        )
    )

    assert payload["success"] is False
    assert "private, internal, or control-plane" in payload["error"]


def test_read_only_navigation_policy_allows_operator_approved_private_target(monkeypatch):
    monkeypatch.setattr(browser_tool, "_is_always_blocked_url", lambda _url: False)
    monkeypatch.setattr(browser_tool, "_is_safe_url", lambda _url: False)
    monkeypatch.setattr(browser_tool, "_allow_private_urls", lambda: True)

    assert browser_tool._read_only_browser_navigate_check(
        {"url": "http://127.0.0.1:8000/approved"}
    ) is True


def test_read_only_browser_uses_separate_process_scoped_session_namespace():
    assert browser_tool._read_only_browser_task_id("turn-7", "read_only") == "turn-7::read-only"
    assert browser_tool._read_only_browser_task_id("turn-7", "action") == "turn-7"
    entry = browser_tool.registry.get_entry("browser_navigate")
    assert "not a cookie-isolation boundary" in entry.schema["description"]


def test_read_only_navigation_blocks_private_redirect_on_local_backend(monkeypatch):
    public = "https://example.com"
    private = "http://127.0.0.1:9000/gateway"
    commands = []
    monkeypatch.setattr(browser_tool, "_is_local_backend", lambda: True)
    monkeypatch.setattr(browser_tool, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(browser_tool, "_is_always_blocked_url", lambda _url: False)
    monkeypatch.setattr(browser_tool, "_is_safe_url", lambda url: url == public)
    monkeypatch.setattr(browser_tool, "_allow_private_urls", lambda: False)
    monkeypatch.setattr(browser_tool, "_navigation_session_key", lambda task_id, _url: task_id)
    monkeypatch.setattr(
        browser_tool,
        "_get_session_info",
        lambda _key: {"_first_nav": False},
    )
    monkeypatch.setattr(browser_tool, "_maybe_start_recording", lambda _key: None)

    def run(_task_id, command, args, **_kwargs):
        commands.append((command, args))
        if command == "open" and args == [public]:
            return {"success": True, "data": {"url": private, "title": "internal"}}
        return {"success": True, "data": {}}

    monkeypatch.setattr(browser_tool, "_run_browser_command", run)

    payload = json.loads(
        browser_tool.browser_navigate(
            public,
            task_id="turn-8::read-only",
            read_only=True,
        )
    )

    assert payload["success"] is False
    assert commands[-1] == ("open", ["about:blank"])
