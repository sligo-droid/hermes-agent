import json

from tools import browser_tool


def test_browser_navigate_returns_plain_json_without_snapshot_or_console(monkeypatch):
    url = "https://status.example.com/health"
    commands = []
    monkeypatch.setattr(browser_tool, "_is_local_backend", lambda: True)
    monkeypatch.setattr(browser_tool, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(browser_tool, "_is_always_blocked_url", lambda _url: False)
    monkeypatch.setattr(browser_tool, "_is_safe_url", lambda _url: True)
    monkeypatch.setattr(browser_tool, "_allow_private_urls", lambda: False)
    monkeypatch.setattr(
        browser_tool,
        "_navigation_session_key",
        lambda task_id, _url: task_id,
    )
    monkeypatch.setattr(
        browser_tool,
        "_get_session_info",
        lambda _key: {"_first_nav": False},
    )

    def run(_task_id, command, args, **_kwargs):
        commands.append(command)
        if command == "open":
            return {"success": True, "data": {"url": url, "title": ""}}
        if command == "eval":
            return {
                "success": True,
                "data": {
                    "result": json.dumps(
                        {
                            "contentType": "application/json; charset=utf-8",
                            "status": 200,
                            "text": '{"status":"ok","revision":"abc123"}',
                        }
                    )
                },
            }
        raise AssertionError(f"unexpected browser command: {command} {args}")

    monkeypatch.setattr(browser_tool, "_run_browser_command", run)

    payload = json.loads(
        browser_tool.browser_navigate(
            url,
            task_id="json-turn::read-only",
            read_only=True,
        )
    )

    assert payload["success"] is True
    assert payload["http_status"] == 200
    assert payload["content_type"] == "application/json"
    assert payload["json"] == {"status": "ok", "revision": "abc123"}
    assert commands == ["open", "eval"]
    assert "snapshot" not in payload


def test_plain_json_probe_bounds_large_response(monkeypatch):
    body = json.dumps({"payload": "x" * browser_tool.PLAIN_JSON_OBSERVATION_LIMIT})
    monkeypatch.setattr(
        browser_tool,
        "_run_browser_command",
        lambda *_args, **_kwargs: {
            "success": True,
            "data": {
                "result": json.dumps(
                    {
                        "contentType": "application/health+json",
                        "status": 200,
                        "text": body,
                    }
                )
            },
        },
    )

    observation = browser_tool._plain_json_document_observation("task")

    assert observation is not None
    assert observation["content_type"] == "application/health+json"
    assert observation["json_truncated"] is True
    assert len(observation["body"]) <= browser_tool.PLAIN_JSON_OBSERVATION_LIMIT
