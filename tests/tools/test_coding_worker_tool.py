from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent.transports.codex_app_server_session import TurnResult
from tools import coding_worker_tool as cwt


class FakeSession:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.closed = False
        self.run_calls = []
        FakeSession.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True

    def run_turn(self, **kwargs):
        self.run_calls.append(kwargs)
        return TurnResult(
            final_text="Changed src/app.py and ran pytest.",
            thread_id="thread-1",
            turn_id="turn-1",
            tool_iterations=2,
        )


def _parent(tmp_path, api_mode="chat_completions"):
    return SimpleNamespace(
        api_mode=api_mode,
        session_cwd=str(tmp_path),
        _touch_activity=lambda message: None,
    )


def test_requires_parent_agent():
    result = json.loads(cwt.delegate_coding_task(task="fix bug"))
    assert "requires a parent agent" in result["error"]


def test_unavailable_inside_codex_app_server(tmp_path):
    result = json.loads(
        cwt.delegate_coding_task(
            task="fix bug",
            parent_agent=_parent(tmp_path, api_mode="codex_app_server"),
        )
    )
    assert "unavailable" in result["error"]


def test_runs_codex_app_server_session(monkeypatch, tmp_path):
    FakeSession.instances = []
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )
    monkeypatch.setattr(cwt, "_load_coding_worker_timeout", lambda: 123.0)

    result = json.loads(
        cwt.delegate_coding_task(
            task="fix the parser",
            context="focus on src/parser.py",
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True
    assert result["summary"] == "Changed src/app.py and ran pytest."
    assert result["cwd"] == str(tmp_path)
    assert FakeSession.instances[0].kwargs["cwd"] == str(tmp_path)
    assert FakeSession.instances[0].run_calls[0]["turn_timeout"] == 123.0
    prompt = FakeSession.instances[0].run_calls[0]["user_input"]
    assert "fix the parser" in prompt
    assert "focus on src/parser.py" in prompt


def test_delegate_uses_opencode_backend_when_configured(monkeypatch, tmp_path):
    from agent import opencode_worker as ow

    monkeypatch.setattr(ow, "load_coding_worker_backend", lambda: ow.BACKEND_OPENCODE)

    def fake_run(prompt, workspace, **kwargs):
        assert "fix the parser" in prompt
        assert workspace == str(tmp_path)
        assert kwargs["context_for_classification"]
        return SimpleNamespace(
            final_text="Changed src/parser.py and ran pytest.",
            error=None,
            interrupted=False,
            agents=["build"],
            plan_text="",
            thread_id="ses-build",
            turn_id="ses-build",
            tool_iterations=1,
        )

    monkeypatch.setattr(ow, "run_opencode_task", fake_run)

    result = json.loads(
        cwt.delegate_coding_task(
            task="fix the parser",
            context="focus on src/parser.py",
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True
    assert result["backend"] == "opencode"
    assert result["agents"] == ["build"]
    assert result["summary"] == "Changed src/parser.py and ran pytest."


def test_runs_with_available_codex_pool_credential(monkeypatch, tmp_path):
    FakeSession.instances = []
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    (hermes_home / "auth.json").write_text(
        json.dumps(
            {
                "version": 1,
                "credential_pool": {
                    "openai-codex": [
                        {
                            "id": "cred-1",
                            "label": "primary",
                            "auth_type": "oauth",
                            "priority": 0,
                            "source": "manual:device_code",
                            "access_token": "access-1",
                            "refresh_token": "refresh-1",
                            "last_status": "exhausted",
                            "last_status_at": time.time(),
                            "last_error_code": 429,
                            "last_error_reset_at": time.time() + 5 * 3600,
                        },
                        {
                            "id": "cred-2",
                            "label": "secondary",
                            "auth_type": "oauth",
                            "priority": 1,
                            "source": "manual:device_code",
                            "access_token": "access-2",
                            "refresh_token": "refresh-2",
                        },
                    ]
                },
            }
        )
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="fix the parser",
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True
    codex_home = FakeSession.instances[0].kwargs["codex_home"]
    assert codex_home
    payload = json.loads((Path(codex_home) / "auth.json").read_text())
    assert payload["tokens"]["access_token"] == "access-2"
    assert payload["tokens"]["refresh_token"] == "refresh-2"


def test_codex_worker_home_prefers_parent_current_credential(tmp_path):
    from agent.codex_worker_auth import prepare_codex_worker_home

    current_entry = SimpleNamespace(
        id="cred-current",
        access_token="access-current",
        refresh_token="refresh-current",
        last_status=None,
    )
    pool = SimpleNamespace(
        current=lambda: current_entry,
        select=MagicMock(),
    )
    parent = SimpleNamespace(provider="openai-codex", _credential_pool=pool)

    credential_id = prepare_codex_worker_home(tmp_path / "codex-home", parent_agent=parent)

    payload = json.loads((tmp_path / "codex-home" / "auth.json").read_text())
    assert credential_id == "cred-current"
    assert payload["tokens"]["access_token"] == "access-current"
    assert payload["tokens"]["refresh_token"] == "refresh-current"
    pool.select.assert_not_called()
