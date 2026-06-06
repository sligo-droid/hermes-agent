from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hermes_cli import web_server
from hermes_cli.dashboard_auth import clear_providers, register_provider
from hermes_cli.dashboard_auth.ws_tickets import _reset_for_tests
from tests.hermes_cli.conftest_dashboard_auth import StubAuthProvider


pytestmark = pytest.mark.xdist_group("dashboard_auth_app_state")


@pytest.fixture
def gated_app(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "hermes"))
    monkeypatch.setenv("HERMES_PUBLIC_KANBAN_BASE_URL", "https://example.test")

    _reset_for_tests()
    clear_providers()
    register_provider(StubAuthProvider())
    prev_host = getattr(web_server.app.state, "bound_host", None)
    prev_port = getattr(web_server.app.state, "bound_port", None)
    prev_required = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.bound_host = "hermes.sligolabs.com"
    web_server.app.state.bound_port = 443
    web_server.app.state.auth_required = True
    client = TestClient(web_server.app, base_url="https://hermes.sligolabs.com")
    yield client
    clear_providers()
    _reset_for_tests()
    web_server.app.state.bound_host = prev_host
    web_server.app.state.bound_port = prev_port
    web_server.app.state.auth_required = prev_required


def _logged_in(client: TestClient) -> None:
    r1 = client.get("/auth/login?provider=stub", follow_redirects=False)
    assert r1.status_code == 302
    state = r1.headers["location"].split("state=")[1]
    r2 = client.get(
        f"/auth/callback?code=stub_code&state={state}",
        follow_redirects=False,
    )
    assert r2.status_code == 302


def test_worker_console_api_accepts_gated_cookie_session(gated_app, tmp_path: Path):
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db

    board = dwb.set_goal(thread_id="1509932516256120842", goal="Inspect console")
    workspace = tmp_path / "repo"
    workspace.mkdir()
    conn = kanban_db.connect(board=board.slug)
    try:
        task = kanban_db.list_tasks(conn, include_archived=False)[0]
        conn.execute(
            "UPDATE tasks SET workspace_kind = 'dir', workspace_path = ? WHERE id = ?",
            (str(workspace), task.id),
        )
        conn.commit()
    finally:
        conn.close()
    log_path = kanban_db.worker_log_path(task.id, board=board.slug)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    secret = "sk-testsecret1234567890"
    kanban_db._append_worker_log_line(log_path, f"operator console log line {secret}")
    from hermes_cli.discord_worker_state import codex_worker_state_path

    state_path = codex_worker_state_path(task.id, board=board.slug)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "events": [
                    {
                        "method": "item/agentMessage/delta",
                        "payload": {"params": {"item": {"type": "agentMessage", "delta": secret}}},
                    }
                ],
                "tool_trace": [
                    {
                        "tool": "commandExecution",
                        "command": "print secret",
                        "status": "completed",
                        "exit_code": 0,
                        "output": secret,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    _logged_in(gated_app)
    resp = gated_app.get(
        f"/api/workers/1509932516256120842/tickets/{task.id}/console"
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["task"]["id"] == task.id
    assert data["workspace"]["available"] is True
    assert "operator console log line" in data["worker_log_tail"]
    serialized = json.dumps(data)
    assert secret not in serialized
    assert "[tool trace]" in data["operator_console_text"]
    assert "[command completed]" in data["operator_console_text"]
    assert "print secret" in data["operator_console_text"]


def test_worker_console_log_accepts_gated_ws_ticket(gated_app, monkeypatch, tmp_path: Path):
    captured: dict[str, object] = {}
    log_path = tmp_path / "worker.log"
    state_path = tmp_path / "worker.codex-state.json"
    log_path.write_text("gated-worker-console-log-ok\n", encoding="utf-8")
    state_path.write_text(
        json.dumps(
            {
                "events": [
                    {
                        "method": "opencode/message",
                        "payload": {"params": {"item": {"text": "gated-event-ok"}}},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    def fake_resolve(session_id: str, task_id: str):
        captured["session_id"] = session_id
        captured["task_id"] = task_id
        return (
            log_path,
            state_path,
            {
                "task": {"id": task_id, "title": "Ticket", "status": "running"},
                "backend": "opencode",
                "workspace": {"path": str(tmp_path), "available": True},
                "current_run": {"id": 1, "worker_pid": 123},
            },
        )

    monkeypatch.setattr(web_server, "_resolve_worker_console_log", fake_resolve)

    _logged_in(gated_app)
    ticket = gated_app.post("/api/auth/ws-ticket").json()["ticket"]
    with gated_app.websocket_connect(
        f"/api/workers/sess-1/tickets/t_1/console/pty?ticket={ticket}",
        headers={"host": "hermes.sligolabs.com"},
    ) as conn:
        buf = b""
        import time

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                frame = conn.receive_bytes()
            except Exception:
                break
            if frame:
                buf += frame
            if b"gated-worker-console-log-ok" in buf and b"gated-event-ok" in buf:
                break
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write("gated-worker-console-later-ok\n")
        state_path.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "method": "opencode/message",
                            "payload": {"params": {"item": {"text": "gated-event-ok"}}},
                        },
                        {
                            "method": "opencode/message",
                            "payload": {
                                "params": {"item": {"text": "gated-event-later-ok"}}
                            },
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                frame = conn.receive_bytes()
            except Exception:
                break
            if frame:
                buf += frame
            if b"gated-worker-console-later-ok" in buf and b"gated-event-later-ok" in buf:
                break

    assert captured["session_id"] == "sess-1"
    assert captured["task_id"] == "t_1"
    assert b"gated-worker-console-log-ok" in buf
    assert b"gated-event-ok" in buf
    assert b"gated-worker-console-later-ok" in buf
    assert b"gated-event-later-ok" in buf
