import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from gateway import memory_telemetry as mt


def _write_proc(root: Path, pid: int, *, ppid: int, rss_kb: int, name: str, cmdline: str) -> None:
    proc = root / str(pid)
    proc.mkdir(parents=True)
    proc.joinpath("status").write_text(
        f"Name:\t{name}\nPid:\t{pid}\nPPid:\t{ppid}\nVmRSS:\t{rss_kb} kB\n",
        encoding="utf-8",
    )
    proc.joinpath("cmdline").write_bytes(cmdline.encode("utf-8") + b"\0")


def _systemctl_empty(args, *, timeout=2.0):
    return SimpleNamespace(returncode=0, stdout="", stderr="")


def test_collects_gateway_and_proc_child_rss(tmp_path):
    proc = tmp_path / "proc"
    _write_proc(proc, 100, ppid=1, rss_kb=2048, name="python", cmdline="python -m hermes_cli.main gateway run")
    _write_proc(proc, 110, ppid=100, rss_kb=4096, name="node", cmdline="node lsp-server")
    _write_proc(proc, 111, ppid=110, rss_kb=1024, name="helper", cmdline="helper --token=super-secret")

    telemetry = mt.collect_gateway_memory_telemetry([100], proc_root=proc, systemctl_runner=_systemctl_empty)

    assert telemetry.gateway_rss_kb == 2048
    assert telemetry.child_rss_kb == 5120
    assert [child.pid for child in telemetry.top_children] == [110, 111]
    assert telemetry.top_children[1].label == "helper --token=[redacted]"


def test_collects_systemd_child_scope_metadata(tmp_path):
    proc = tmp_path / "proc"
    _write_proc(proc, 100, ppid=1, rss_kb=2048, name="python", cmdline="gateway")
    _write_proc(proc, 200, ppid=1, rss_kb=8192, name="pyright", cmdline="pyright-langserver --stdio")

    def runner(args, *, timeout=2.0):
        if args[0] == "list-units":
            return SimpleNamespace(
                returncode=0,
                stdout="hermes-gateway-child-lsp-session-pyright-123.scope loaded active running x\n",
                stderr="",
            )
        assert args[:2] == ["show", "hermes-gateway-child-lsp-session-pyright-123.scope"]
        return SimpleNamespace(
            returncode=0,
            stdout="MainPID=200\nDescription=Hermes gateway child lsp: LSP server pyright\n",
            stderr="",
        )

    telemetry = mt.collect_gateway_memory_telemetry([100], proc_root=proc, systemctl_runner=runner)

    assert telemetry.source == "systemd+/proc"
    assert telemetry.child_rss_kb == 8192
    child = telemetry.top_children[0]
    assert child.pid == 200
    assert child.kind == "lsp"
    assert child.unit == "hermes-gateway-child-lsp-session-pyright-123.scope"
    assert child.label == "lsp: LSP server pyright"


def test_collects_codex_app_server_scope_as_child_helper_rss(tmp_path):
    proc = tmp_path / "proc"
    _write_proc(proc, 100, ppid=1, rss_kb=2048, name="python", cmdline="gateway")
    _write_proc(proc, 200, ppid=1, rss_kb=16384, name="systemd-run", cmdline="systemd-run --scope codex app-server")
    _write_proc(proc, 201, ppid=200, rss_kb=32768, name="codex", cmdline="codex app-server")

    def runner(args, *, timeout=2.0):
        if args[0] == "list-units":
            return SimpleNamespace(
                returncode=0,
                stdout="hermes-gateway-child-codex-app-server-discord-123-codex-app-server-999.scope loaded active running x\n",
                stderr="",
            )
        assert args[:2] == ["show", "hermes-gateway-child-codex-app-server-discord-123-codex-app-server-999.scope"]
        return SimpleNamespace(
            returncode=0,
            stdout="MainPID=200\nDescription=Hermes gateway child codex-app-server: Codex app-server runtime\n",
            stderr="",
        )

    telemetry = mt.collect_gateway_memory_telemetry([100], proc_root=proc, systemctl_runner=runner)

    assert telemetry.child_rss_kb == 49152
    child = telemetry.top_children[0]
    assert child.pid == 200
    assert child.kind == "codex"
    assert child.unit == "hermes-gateway-child-codex-app-server-discord-123-codex-app-server-999.scope"
    assert child.label == "codex-app-server: Codex app-server runtime"


def test_systemd_child_scope_counts_descendant_rss(tmp_path):
    proc = tmp_path / "proc"
    _write_proc(proc, 100, ppid=1, rss_kb=2048, name="python", cmdline="gateway")
    _write_proc(proc, 200, ppid=1, rss_kb=8192, name="worker", cmdline="worker")
    _write_proc(proc, 201, ppid=200, rss_kb=4096, name="helper", cmdline="helper --secret:value")

    def runner(args, *, timeout=2.0):
        if args[0] == "list-units":
            return SimpleNamespace(
                returncode=0,
                stdout="hermes-gateway-child-terminal-session-worker-123.scope loaded active running x\n",
                stderr="",
            )
        assert args[:2] == ["show", "hermes-gateway-child-terminal-session-worker-123.scope"]
        return SimpleNamespace(
            returncode=0,
            stdout="MainPID=200\nDescription=Hermes gateway child terminal: coding worker\n",
            stderr="",
        )

    telemetry = mt.collect_gateway_memory_telemetry([100], proc_root=proc, systemctl_runner=runner)

    assert telemetry.child_rss_kb == 12288
    assert telemetry.top_children[0].rss_kb == 12288


def test_tolerates_missing_systemd_and_exited_child(tmp_path):
    proc = tmp_path / "proc"
    _write_proc(proc, 100, ppid=1, rss_kb=2048, name="python", cmdline="gateway")

    def runner(args, *, timeout=2.0):
        if args[0] == "list-units":
            raise subprocess.TimeoutExpired(args, timeout)
        raise AssertionError(args)

    telemetry = mt.collect_gateway_memory_telemetry([100], proc_root=proc, systemctl_runner=runner)

    assert telemetry.gateway_rss_kb == 2048
    assert telemetry.child_rss_kb == 0
    assert telemetry.top_children == ()
    assert telemetry.warnings


def test_format_gateway_memory_lines_identifies_kill_target():
    telemetry = mt.GatewayMemoryTelemetry(
        gateway_pids=(100,),
        gateway_rss_kb=1024,
        child_rss_kb=2048,
        top_children=(
            mt.ChildMemory(
                pid=200,
                rss_kb=2048,
                kind="terminal",
                label="sleep 999",
                unit="hermes-gateway-child-terminal-session-sleep.scope",
                source="systemd",
            ),
        ),
        source="systemd+/proc",
    )

    lines = mt.format_gateway_memory_lines(telemetry)

    assert "Gateway RSS: 1.0 MiB" in lines
    assert "Isolated child/helper RSS: 2.0 MiB" in lines
    assert any("PID 200 terminal unit=hermes-gateway-child-terminal-session-sleep.scope" in line for line in lines)


def test_killing_child_process_leaves_parent_gateway_fixture_alive():
    parent = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import subprocess,sys,time,os; "
                "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
                "print(child.pid, flush=True); "
                "time.sleep(60)"
            ),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert parent.stdout is not None
        child_pid = int(parent.stdout.readline().strip())
        child = subprocess.Popen(["/bin/kill", str(child_pid)])
        child.wait(timeout=5)
        time.sleep(0.2)
        assert parent.poll() is None
    finally:
        parent.terminate()
        try:
            parent.wait(timeout=5)
        except subprocess.TimeoutExpired:
            parent.kill()
            parent.wait(timeout=5)
