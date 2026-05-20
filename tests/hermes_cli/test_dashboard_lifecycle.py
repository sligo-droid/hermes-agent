from __future__ import annotations

import json
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import hermes_cli.dashboard_lifecycle as lifecycle


def test_record_dashboard_runtime_writes_launch_metadata():
    lifecycle.record_dashboard_runtime(
        argv=["python", "-m", "hermes_cli.main", "dashboard", "--port", "9119"],
        cwd="/tmp/hermes",
        host="127.0.0.1",
        port=9119,
        skip_build=True,
        tui=False,
        insecure=False,
    )

    data = json.loads(lifecycle.dashboard_runtime_path().read_text(encoding="utf-8"))
    assert data["argv"] == [
        "python",
        "-m",
        "hermes_cli.main",
        "dashboard",
        "--port",
        "9119",
    ]
    assert data["cwd"] == "/tmp/hermes"
    assert data["host"] == "127.0.0.1"
    assert data["port"] == 9119
    assert data["skip_build"] is True
    assert isinstance(data["pid"], int)
    assert isinstance(data["started_at"], float)


def test_record_dashboard_runtime_makes_python_source_argv_restartable(tmp_path):
    script = tmp_path / "main.py"
    script.write_text("print('dashboard')\n", encoding="utf-8")

    lifecycle.record_dashboard_runtime(
        argv=[str(script), "dashboard", "--port", "9119"],
        cwd="/tmp/hermes",
        host="127.0.0.1",
        port=9119,
        skip_build=True,
        tui=False,
        insecure=False,
    )

    data = json.loads(lifecycle.dashboard_runtime_path().read_text(encoding="utf-8"))
    assert data["argv"] == [
        sys.executable,
        str(script),
        "dashboard",
        "--port",
        "9119",
    ]


def test_scan_dashboard_launches_reads_argv_and_cwd_from_proc(monkeypatch):
    monkeypatch.setattr(lifecycle.os, "getpid", lambda: 999)
    monkeypatch.setattr(
        lifecycle.subprocess,
        "run",
        lambda *args, **kwargs: MagicMock(
            returncode=0,
            stdout=(
                "  123 python -m hermes_cli.main dashboard --host 127.0.0.1\n"
                "  456 python -m hermes_cli.main chat -q dashboard\n"
            ),
            stderr="",
        ),
    )
    monkeypatch.setattr(
        lifecycle,
        "_read_proc_argv",
        lambda pid: ["python", "-m", "hermes_cli.main", "dashboard"]
        if pid == 123
        else None,
    )
    monkeypatch.setattr(lifecycle, "_read_proc_cwd", lambda pid: "/srv/hermes")

    launches = lifecycle._scan_dashboard_launches()

    assert len(launches) == 1
    assert launches[0].pid == 123
    assert launches[0].argv == ["python", "-m", "hermes_cli.main", "dashboard"]
    assert launches[0].cwd == "/srv/hermes"


def test_runtime_launch_ignores_stale_pid(monkeypatch):
    lifecycle.dashboard_runtime_path().parent.mkdir(parents=True, exist_ok=True)
    lifecycle.dashboard_runtime_path().write_text(
        json.dumps(
            {
                "pid": 123,
                "argv": ["python", "-m", "hermes_cli.main", "dashboard"],
                "cwd": "/srv/hermes",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(lifecycle, "_pid_exists", lambda pid: False)

    assert lifecycle._runtime_launch() is None


def test_restart_dashboard_uses_best_launch(monkeypatch, capsys):
    calls = []
    launch = lifecycle.DashboardLaunch(
        pid=123,
        argv=["python", "-m", "hermes_cli.main", "dashboard"],
        cwd="/srv/hermes",
        source="runtime",
    )
    monkeypatch.setattr(lifecycle, "_best_restart_launch", lambda: launch)
    monkeypatch.setattr(
        lifecycle,
        "stop_dashboard_processes",
        lambda **kwargs: calls.append(("stop", kwargs)) or True,
    )
    monkeypatch.setattr(
        lifecycle,
        "_spawn_dashboard",
        lambda received: calls.append(("spawn", received))
        or SimpleNamespace(pid=456),
    )

    assert lifecycle.restart_dashboard_if_running(reason="test restart") is True

    assert calls == [
        ("stop", {"reason": "test restart", "print_restart_hint": False}),
        ("spawn", launch),
    ]
    assert "Restarted dashboard (PID 456)" in capsys.readouterr().out


def test_restart_dashboard_missing_can_be_quiet(monkeypatch, capsys):
    monkeypatch.setattr(lifecycle, "_best_restart_launch", lambda: None)

    assert lifecycle.restart_dashboard_if_running(quiet_when_missing=True) is False

    assert capsys.readouterr().out == ""


def test_spawn_dashboard_detaches_and_logs(monkeypatch, tmp_path):
    launch = lifecycle.DashboardLaunch(
        pid=123,
        argv=["python", "-m", "hermes_cli.main", "dashboard"],
        cwd=str(tmp_path),
        source="runtime",
    )
    calls = []

    def fake_popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(pid=456)

    monkeypatch.setattr(lifecycle.subprocess, "Popen", fake_popen)

    proc = lifecycle._spawn_dashboard(launch)

    assert proc.pid == 456
    assert calls[0][0] == launch.argv
    assert calls[0][1]["cwd"] == str(tmp_path)
    assert calls[0][1]["stdin"] is subprocess.DEVNULL
    assert calls[0][1]["stderr"] is subprocess.STDOUT
    assert calls[0][1]["start_new_session"] is True


def test_spawn_dashboard_repairs_older_python_source_runtime(monkeypatch, tmp_path):
    script = tmp_path / "main.py"
    script.write_text("print('dashboard')\n", encoding="utf-8")
    launch = lifecycle.DashboardLaunch(
        pid=123,
        argv=[str(script), "dashboard"],
        cwd=str(tmp_path),
        source="runtime",
    )
    calls = []

    def fake_popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(pid=456)

    monkeypatch.setattr(lifecycle.subprocess, "Popen", fake_popen)

    lifecycle._spawn_dashboard(launch)

    assert calls[0][0] == [sys.executable, str(script), "dashboard"]
