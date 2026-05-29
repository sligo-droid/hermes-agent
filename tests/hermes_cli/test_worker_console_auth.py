from __future__ import annotations

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
    kanban_db._append_worker_log_line(log_path, "operator console log line")

    _logged_in(gated_app)
    resp = gated_app.get(
        f"/api/workers/1509932516256120842/tickets/{task.id}/console"
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["task"]["id"] == task.id
    assert data["workspace"]["available"] is True
    assert "operator console log line" in data["worker_log_tail"]


def test_worker_console_pty_accepts_gated_ws_ticket(gated_app, monkeypatch):
    captured: dict[str, object] = {}

    def fake_resolve(session_id: str, task_id: str):
        captured["session_id"] = session_id
        captured["task_id"] = task_id
        return (["worker-shell"], "/tmp", {"HERMES_WORKER_CONSOLE": "1"})

    class FakeBridge:
        def __init__(self):
            self.sent = False

        def read(self, _timeout: float):
            if self.sent:
                return None
            self.sent = True
            return b"gated-worker-console-ok"

        def write(self, _raw: bytes) -> None:
            return None

        def resize(self, *, cols: int, rows: int) -> None:
            captured["resize"] = (cols, rows)

        def close(self) -> None:
            captured["closed"] = True

    class FakePtyBridge:
        @classmethod
        def spawn(cls, argv, *, cwd=None, env=None):
            captured["spawn"] = {"argv": argv, "cwd": cwd, "env": env}
            return FakeBridge()

    monkeypatch.setattr(web_server, "_resolve_worker_console_argv", fake_resolve)
    monkeypatch.setattr(web_server, "_PTY_BRIDGE_AVAILABLE", True)
    monkeypatch.setattr(web_server, "PtyBridge", FakePtyBridge)

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
            if b"gated-worker-console-ok" in buf:
                break

    assert captured["session_id"] == "sess-1"
    assert captured["task_id"] == "t_1"
    assert captured["spawn"] == {
        "argv": ["worker-shell"],
        "cwd": "/tmp",
        "env": {"HERMES_WORKER_CONSOLE": "1"},
    }
    assert b"gated-worker-console-ok" in buf
