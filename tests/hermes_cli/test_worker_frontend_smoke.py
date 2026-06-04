from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest


def test_smoke_helper_succeeds_on_exact_port_and_cleans_up(monkeypatch):
    from hermes_cli import worker_frontend_smoke as smoke

    calls = {"terminated": 0, "waited": 0, "urls": []}

    class Proc:
        def terminate(self):
            calls["terminated"] += 1

        def wait(self, timeout=None):
            calls["waited"] += 1

    popen_kwargs = {}

    def fake_popen(*args, **kwargs):
        popen_kwargs.update(kwargs)
        return Proc()

    monkeypatch.setattr(smoke.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(smoke, "wait_for_url", lambda url, **kwargs: calls["urls"].append(url))

    def fake_fetch(url, *, timeout):
        calls["urls"].append(url)
        return 200, "hello smoke"

    monkeypatch.setattr(smoke, "fetch_url", fake_fetch)

    smoke.run_smoke(
        url="http://127.0.0.1:4173",
        cmd="pnpm preview --host 127.0.0.1 --port 4173",
        routes=[smoke.Probe("/", expect_text="hello")],
    )

    assert calls["urls"] == ["http://127.0.0.1:4173", "http://127.0.0.1:4173/"]
    assert calls["terminated"] == 1
    assert calls["waited"] == 1
    assert popen_kwargs["start_new_session"] is True


def test_smoke_helper_rejects_mismatched_probe_before_start(monkeypatch):
    from hermes_cli import worker_frontend_smoke as smoke

    monkeypatch.setattr(
        smoke.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("preview command must not start for mismatched route"),
    )

    with pytest.raises(ValueError, match="exact preview host:port"):
        smoke.run_smoke(
            url="http://127.0.0.1:4173",
            cmd="pnpm preview --port 4173",
            routes=[smoke.Probe("http://127.0.0.1:5173/")],
        )


def test_smoke_helper_cli_reports_mismatch(monkeypatch, capsys):
    from hermes_cli import worker_frontend_smoke as smoke

    monkeypatch.setattr(smoke.subprocess, "Popen", lambda *args, **kwargs: SimpleNamespace())

    code = smoke.main(
        [
            "--url",
            "http://127.0.0.1:4173",
            "--cmd",
            f"{sys.executable} -m http.server 4173",
            "--route",
            "http://localhost:4173/",
        ]
    )

    assert code == 1
    assert "exact preview host:port" in capsys.readouterr().err
