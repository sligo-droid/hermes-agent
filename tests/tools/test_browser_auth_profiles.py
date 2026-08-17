import asyncio
import json
import threading

import pytest

from tools import browser_tool
from tools.browser_auth_profiles import (
    BrowserAuthProfileError,
    load_browser_auth_credentials,
    matching_browser_auth_profile_names,
    select_browser_auth_profile,
)
from tools.browser_supervisor import CDPSupervisor, _preferred_page_target
from tools.registry import registry


def _profile_config(env_file):
    return {
        "browser": {
            "auth_profiles": {
                "pid_hermes_qa": {
                    "origins": ["https://protected.example.com"],
                    "env_file": str(env_file),
                    "username_env": "PID_QA_USERNAME",
                    "password_env": "PID_QA_PASSWORD",
                    "username_selector": "#login-user",
                    "password_selector": "#login-pass",
                    "submit_selector": '#login-overlay button[type="submit"]',
                    "success_selector": "#header",
                }
            }
        }
    }


def _preview_profile_config(env_file):
    config = _profile_config(env_file)
    profile = config["browser"]["auth_profiles"]["pid_hermes_qa"]
    profile["origins"] = []
    profile["origin_patterns"] = [
        "https://pid-git-*-sligo-labs.vercel.app",
    ]
    return config


def test_supervisor_prefers_navigated_page_over_initial_blank_target():
    targets = [
        {"targetId": "blank", "type": "page", "url": "about:blank"},
        {
            "targetId": "pid",
            "type": "page",
            "url": "https://protected.example.com/login",
        },
    ]

    assert _preferred_page_target(targets)["targetId"] == "pid"
    assert _preferred_page_target(
        targets,
        expected_url="https://protected.example.com/",
    )["targetId"] == "pid"


def test_profile_loads_private_hermes_secret_without_exposing_values(
    tmp_path, monkeypatch
):
    hermes_home = tmp_path / ".hermes"
    secrets = hermes_home / "secrets"
    secrets.mkdir(parents=True)
    env_file = secrets / "pid-qa-readonly.env"
    env_file.write_text(
        "PID_QA_USERNAME=hermes_qa\nPID_QA_PASSWORD='correct horse battery staple'\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    profile = select_browser_auth_profile(
        "https://protected.example.com",
        config=_profile_config(env_file),
    )
    username, password = load_browser_auth_credentials(profile)

    assert profile.name == "pid_hermes_qa"
    assert username == "hermes_qa"
    assert password == "correct horse battery staple"
    assert password not in repr(profile)


def test_matching_profiles_returns_only_valid_exact_origin_profiles(
    tmp_path, monkeypatch
):
    hermes_home = tmp_path / ".hermes"
    secrets = hermes_home / "secrets"
    secrets.mkdir(parents=True)
    env_file = secrets / "pid-qa-readonly.env"
    env_file.write_text(
        "PID_QA_USERNAME=hermes_qa\nPID_QA_PASSWORD=secret\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    config = _profile_config(env_file)
    config["browser"]["auth_profiles"]["invalid"] = {
        "origins": ["https://protected.example.com"],
        "env_file": str(tmp_path / "missing.env"),
    }

    assert matching_browser_auth_profile_names(
        "https://protected.example.com", config=config
    ) == ("pid_hermes_qa",)
    assert matching_browser_auth_profile_names(
        "https://example.com", config=config
    ) == ()


def test_profile_matches_dynamic_loopback_port_pattern(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    secrets = hermes_home / "secrets"
    secrets.mkdir(parents=True)
    env_file = secrets / "pid-qa-readonly.env"
    env_file.write_text(
        "PID_QA_USERNAME=hermes_qa\nPID_QA_PASSWORD=secret\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    config = _profile_config(env_file)
    profile = config["browser"]["auth_profiles"]["pid_hermes_qa"]
    profile["origins"] = []
    profile["origin_patterns"] = ["http://127.0.0.1:*"]

    assert matching_browser_auth_profile_names(
        "http://127.0.0.1:41649", config=config
    ) == ("pid_hermes_qa",)
    assert matching_browser_auth_profile_names(
        "http://127.0.0.1", config=config
    ) == ()
    assert matching_browser_auth_profile_names(
        "http://localhost:41649", config=config
    ) == ()


def test_profile_matches_narrow_vercel_preview_origin_pattern(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    secrets = hermes_home / "secrets"
    secrets.mkdir(parents=True)
    env_file = secrets / "pid-qa-readonly.env"
    env_file.write_text(
        "PID_QA_USERNAME=hermes_qa\nPID_QA_PASSWORD=secret\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    config = _preview_profile_config(env_file)

    preview_origin = (
        "https://pid-git-discord-action-pid-e3b40e1a059a-"
        "sligo-labs.vercel.app"
    )
    assert matching_browser_auth_profile_names(preview_origin, config=config) == (
        "pid_hermes_qa",
    )
    assert select_browser_auth_profile(preview_origin, config=config).name == (
        "pid_hermes_qa"
    )
    assert matching_browser_auth_profile_names(
        "https://pid-git-main-other-team.vercel.app", config=config
    ) == ()
    assert matching_browser_auth_profile_names(
        "https://pid-preview-sligo-labs.vercel.app", config=config
    ) == ()


@pytest.mark.parametrize(
    "pattern",
    [
        "http://pid-git-*-sligo-labs.vercel.app",
        "https://*.vercel.app",
        "https://pid-git-*-example.com:443",
        "https://pid-git-*-example.com/path",
        "https://pid-*-*.vercel.app",
        "http://0.0.0.0:*",
        "http://127.0.0.*:*",
        "https://127.0.0.1:*",
    ],
)
def test_profile_rejects_broad_or_invalid_origin_patterns(
    tmp_path, monkeypatch, pattern
):
    hermes_home = tmp_path / ".hermes"
    secrets = hermes_home / "secrets"
    secrets.mkdir(parents=True)
    env_file = secrets / "pid-qa-readonly.env"
    env_file.write_text(
        "PID_QA_USERNAME=hermes_qa\nPID_QA_PASSWORD=secret\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    config = _preview_profile_config(env_file)
    config["browser"]["auth_profiles"]["pid_hermes_qa"]["origin_patterns"] = [
        pattern
    ]

    with pytest.raises(BrowserAuthProfileError, match="origin pattern"):
        select_browser_auth_profile(
            "https://pid-preview-sligo-labs.vercel.app",
            requested_name="pid_hermes_qa",
            config=config,
        )


def test_navigation_auth_hint_is_limited_to_matching_sign_in_pages(monkeypatch):
    from tools import browser_auth_profiles

    monkeypatch.setattr(
        browser_auth_profiles,
        "matching_browser_auth_profile_names",
        lambda origin: ("pid_hermes_qa",) if origin == "https://protected.example.com" else (),
    )

    hint = browser_tool._protected_authentication_hint(
        "https://protected.example.com/briefing",
        "Sign In to Briefing Studio",
        '- textbox "Password" [required]',
    )

    assert hint == {
        "available": True,
        "tool": "browser_authenticate",
        "instruction": (
            "This sign-in page has operator-configured QA access. "
            "Call browser_authenticate, then inspect the protected page with browser_snapshot."
        ),
    }
    assert (
        browser_tool._protected_authentication_hint(
            "https://protected.example.com/briefing",
            "Briefing Studio",
            "Authenticated content",
        )
        is None
    )


def test_profile_rejects_non_private_or_outside_secret_files(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    secrets = hermes_home / "secrets"
    secrets.mkdir(parents=True)
    outside = tmp_path / "qa.env"
    outside.write_text("PID_QA_USERNAME=x\nPID_QA_PASSWORD=y\n", encoding="utf-8")
    outside.chmod(0o600)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    with pytest.raises(BrowserAuthProfileError, match="secrets directory"):
        select_browser_auth_profile(
            "https://protected.example.com",
            requested_name="pid_hermes_qa",
            config=_profile_config(outside),
        )

    inside = secrets / "qa.env"
    inside.write_text("PID_QA_USERNAME=x\nPID_QA_PASSWORD=y\n", encoding="utf-8")
    inside.chmod(0o644)
    with pytest.raises(BrowserAuthProfileError, match="private regular file"):
        select_browser_auth_profile(
            "https://protected.example.com",
            requested_name="pid_hermes_qa",
            config=_profile_config(inside),
        )


def test_supervisor_authentication_keeps_secrets_out_of_source_and_result():
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    supervisor = CDPSupervisor(task_id="auth-test", cdp_url="ws://example.test")
    supervisor._loop = loop
    supervisor._active = True
    supervisor._page_session_id = "page-1"
    submitted = False
    calls = []

    async def fake_cdp(method, params=None, **kwargs):
        nonlocal submitted
        calls.append((method, params))
        if method == "Runtime.evaluate" and params.get("expression") == "globalThis":
            return {"result": {"result": {"objectId": "global-1"}}}
        if method == "Runtime.evaluate":
            expression = params.get("expression")
            if expression == "location.origin":
                return {
                    "result": {
                        "result": {
                            "type": "string",
                            "value": "https://protected.example.com",
                        }
                    }
                }
            value = True if expression == "document.readyState === 'complete'" else submitted
            return {"result": {"result": {"type": "boolean", "value": value}}}
        if method == "Runtime.callFunctionOn":
            if "expectedOrigin" in params["functionDeclaration"]:
                return {"result": {"result": {"type": "string", "value": "inserted"}}}
            if "requestSubmit" in params["functionDeclaration"]:
                submitted = True
            return {"result": {"result": {"type": "boolean", "value": True}}}
        raise AssertionError(method)

    supervisor._cdp = fake_cdp
    try:
        result = supervisor.authenticate_form(
            username="hermes_qa",
            password="top-secret-password",
            username_selector="#login-user",
            password_selector="#login-pass",
            submit_selector="button[type=submit]",
            success_selector="#header",
            expected_origin="https://protected.example.com",
            timeout=2,
        )
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()

    assert result == {
        "ok": True,
        "authenticated": True,
        "already_authenticated": False,
    }
    function_sources = [
        params["functionDeclaration"]
        for method, params in calls
        if method == "Runtime.callFunctionOn"
    ]
    assert all("hermes_qa" not in source for source in function_sources)
    assert all("top-secret-password" not in source for source in function_sources)
    insert_arguments = [
        params["arguments"]
        for method, params in calls
        if method == "Runtime.callFunctionOn"
        and "expectedOrigin" in params["functionDeclaration"]
    ]
    assert [arguments[2]["value"] for arguments in insert_arguments] == [
        "hermes_qa",
        "top-secret-password",
    ]
    assert all(method != "Input.insertText" for method, _params in calls)
    assert "hermes_qa" not in json.dumps(result)
    assert "top-secret-password" not in json.dumps(result)


def test_supervisor_authentication_waits_through_login_page_reload():
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    supervisor = CDPSupervisor(task_id="auth-reload-test", cdp_url="ws://example.test")
    supervisor._loop = loop
    supervisor._active = True
    supervisor._page_session_id = "page-1"
    submitted = False
    checks_after_submit = 0

    async def fake_cdp(method, params=None, **kwargs):
        nonlocal submitted, checks_after_submit
        if method == "Runtime.evaluate" and params.get("expression") == "globalThis":
            return {"result": {"result": {"objectId": "global-1"}}}
        if method == "Runtime.evaluate":
            expression = params.get("expression", "")
            if expression == "location.origin":
                return {
                    "result": {
                        "result": {
                            "value": "https://protected.example.com",
                        }
                    }
                }
            if expression == "document.readyState === 'complete'":
                value = True
            elif "aria-invalid" in expression:
                value = False
            elif "#header" in expression:
                if submitted:
                    checks_after_submit += 1
                value = submitted and checks_after_submit >= 3
            else:
                value = False
            return {"result": {"result": {"value": value}}}
        if method == "Runtime.callFunctionOn":
            if "expectedOrigin" in params["functionDeclaration"]:
                return {"result": {"result": {"value": "inserted"}}}
            if "requestSubmit" in params["functionDeclaration"]:
                submitted = True
            return {"result": {"result": {"value": True}}}
        raise AssertionError(method)

    supervisor._cdp = fake_cdp
    try:
        result = supervisor.authenticate_form(
            username="hermes_qa",
            password="top-secret-password",
            username_selector="#login-user",
            password_selector="#login-pass",
            submit_selector="button[type=submit]",
            success_selector="#header",
            expected_origin="https://protected.example.com",
            timeout=2,
        )
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()

    assert result == {
        "ok": True,
        "authenticated": True,
        "already_authenticated": False,
    }
    assert "hermes_qa" not in json.dumps(result)
    assert "top-secret-password" not in json.dumps(result)


def test_supervisor_checks_origin_and_inserts_credentials_atomically():
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    supervisor = CDPSupervisor(task_id="auth-origin-test", cdp_url="ws://example.test")
    supervisor._loop = loop
    supervisor._active = True
    supervisor._page_session_id = "page-1"
    calls = []

    async def fake_cdp(method, params=None, **kwargs):
        calls.append((method, params))
        if method == "Runtime.evaluate" and params.get("expression") == "globalThis":
            return {"result": {"result": {"objectId": "global-1"}}}
        if method == "Runtime.evaluate":
            expression = params.get("expression", "")
            if expression == "document.readyState === 'complete'":
                value = True
            elif expression == "performance.timeOrigin":
                value = 1000.0
            else:
                value = False
            return {"result": {"result": {"value": value}}}
        if method == "Runtime.callFunctionOn":
            if "expectedOrigin" in params["functionDeclaration"]:
                arguments = params["arguments"]
                assert arguments[1]["value"] == "https://protected.example.com"
                assert arguments[2]["value"] == "hermes_qa"
                return {"result": {"result": {"value": "origin_changed"}}}
            return {"result": {"result": {"value": True}}}
        raise AssertionError(method)

    supervisor._cdp = fake_cdp
    try:
        result = supervisor.authenticate_form(
            username="hermes_qa",
            password="top-secret-password",
            username_selector="#login-user",
            password_selector="#login-pass",
            submit_selector="button[type=submit]",
            success_selector="#header",
            expected_origin="https://protected.example.com",
            timeout=2,
        )
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()

    assert result == {
        "ok": False,
        "error": "browser origin changed before protected credentials were inserted",
    }
    assert all(method != "Input.insertText" for method, _params in calls)
    assert not any(
        method == "Runtime.evaluate" and params.get("expression") == "location.origin"
        for method, params in calls
    )
    assert "hermes_qa" not in json.dumps(result)
    assert "top-secret-password" not in json.dumps(result)


def test_browser_authenticate_returns_only_profile_metadata(monkeypatch):
    class FakeSupervisor:
        def select_page(self, url):
            return True

        def current_origin(self):
            return {"ok": True, "origin": "https://protected.example.com"}

        def authenticate_form(self, **kwargs):
            assert kwargs["username"] == "hermes_qa"
            assert kwargs["password"] == "top-secret-password"
            return {"ok": True, "authenticated": True, "already_authenticated": False}

    from tools import browser_auth_profiles
    from tools import browser_supervisor

    profile = type(
        "Profile",
        (),
        {
            "name": "pid_hermes_qa",
            "username_selector": "#user",
            "password_selector": "#pass",
            "submit_selector": "button",
            "success_selector": "#header",
            "timeout_s": 5,
        },
    )()
    monkeypatch.setattr(browser_tool, "_last_session_key", lambda task_id: task_id)
    monkeypatch.setattr(browser_tool, "_ensure_cdp_supervisor", lambda task_id: None)
    monkeypatch.setattr(
        browser_tool,
        "_run_browser_command",
        lambda *args, **kwargs: {"success": False},
    )
    monkeypatch.setattr(
        browser_supervisor.SUPERVISOR_REGISTRY,
        "get",
        lambda task_id: FakeSupervisor(),
    )
    monkeypatch.setattr(
        browser_auth_profiles,
        "select_browser_auth_profile",
        lambda origin, requested_name="": profile,
    )
    monkeypatch.setattr(
        browser_auth_profiles,
        "load_browser_auth_credentials",
        lambda selected: ("hermes_qa", "top-secret-password"),
    )

    result = json.loads(browser_tool.browser_authenticate(task_id="visual-turn"))

    assert result == {
        "success": True,
        "authenticated": True,
        "profile": "pid_hermes_qa",
        "already_authenticated": False,
    }
    assert "top-secret-password" not in json.dumps(result)


def test_browser_authenticate_retargets_supervisor_to_navigated_page(monkeypatch):
    class FakeSupervisor:
        def __init__(self):
            self.current = "null"
            self.selected_urls = []

        def select_page(self, url):
            self.selected_urls.append(url)
            self.current = "https://protected.example.com"
            return True

        def current_origin(self):
            return {"ok": True, "origin": self.current}

        def authenticate_form(self, **kwargs):
            return {"ok": True, "authenticated": True, "already_authenticated": False}

    from tools import browser_auth_profiles
    from tools import browser_supervisor

    supervisor = FakeSupervisor()
    profile = type(
        "Profile",
        (),
        {
            "name": "pid_hermes_qa",
            "username_selector": "#user",
            "password_selector": "#pass",
            "submit_selector": "button",
            "success_selector": "#header",
            "timeout_s": 5,
        },
    )()
    monkeypatch.setattr(browser_tool, "_last_session_key", lambda task_id: task_id)
    monkeypatch.setattr(browser_tool, "_ensure_cdp_supervisor", lambda task_id: None)
    monkeypatch.setattr(
        browser_tool,
        "_run_browser_command",
        lambda *args, **kwargs: {
            "success": True,
            "data": {"result": '"https://protected.example.com/"'},
        },
    )
    monkeypatch.setattr(
        browser_supervisor.SUPERVISOR_REGISTRY,
        "get",
        lambda task_id: supervisor,
    )
    monkeypatch.setattr(
        browser_auth_profiles,
        "select_browser_auth_profile",
        lambda origin, requested_name="": (
            profile
            if origin == "https://protected.example.com"
            else (_ for _ in ()).throw(BrowserAuthProfileError("invalid origin"))
        ),
    )
    monkeypatch.setattr(
        browser_auth_profiles,
        "load_browser_auth_credentials",
        lambda selected: ("hermes_qa", "top-secret-password"),
    )

    result = json.loads(browser_tool.browser_authenticate(task_id="visual-turn"))

    assert result["success"] is True
    assert supervisor.selected_urls == ["https://protected.example.com/"]


def test_browser_authenticate_schema_exposes_only_an_opaque_profile_name():
    entry = registry.get_entry("browser_authenticate")

    assert entry is not None
    assert entry.effect == "read_only"
    parameters = entry.schema["parameters"]
    assert parameters["additionalProperties"] is False
    assert set(parameters["properties"]) == {"profile"}


def test_browser_authenticate_uses_read_only_browser_namespace(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        browser_tool,
        "browser_authenticate",
        lambda profile="", task_id=None: captured.update(
            profile=profile,
            task_id=task_id,
        ) or "{}",
    )

    entry = registry.get_entry("browser_authenticate")
    entry.handler(
        {"profile": "pid_hermes_qa"},
        task_id="turn-7",
        runtime_mode="read_only",
    )

    assert captured == {
        "profile": "pid_hermes_qa",
        "task_id": "turn-7::read-only",
    }


def test_successful_navigation_refreshes_and_aligns_cdp_supervisor(monkeypatch):
    calls = []
    monkeypatch.setattr(browser_tool, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(browser_tool, "_is_local_backend", lambda: True)
    monkeypatch.setattr(browser_tool, "check_website_access", lambda _url: None)
    monkeypatch.setattr(browser_tool, "_get_session_info", lambda _key: {"_first_nav": False})
    monkeypatch.setattr(
        browser_tool,
        "_run_browser_command",
        lambda _task, command, _args, **_kwargs: (
            {"success": True, "data": {"url": "https://app.example.test/", "title": "App"}}
            if command == "open"
            else {"success": False}
        ),
    )
    monkeypatch.setattr(
        browser_tool,
        "_ensure_cdp_supervisor",
        lambda key: calls.append(("ensure", key)),
    )
    monkeypatch.setattr(
        browser_tool,
        "_align_cdp_supervisor_to_current_page",
        lambda key: calls.append(("align", key)) or True,
    )
    monkeypatch.setattr(browser_tool, "_should_probe_plain_json", lambda *_args: False)

    result = json.loads(
        browser_tool.browser_navigate(
            "https://app.example.test/",
            task_id="visual-turn",
        )
    )

    assert result["success"] is True
    assert calls == [("ensure", "visual-turn"), ("align", "visual-turn")]
