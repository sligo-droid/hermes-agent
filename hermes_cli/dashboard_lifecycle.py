"""Lifecycle helpers for the standalone ``hermes dashboard`` process."""

from __future__ import annotations

import errno
import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path
from typing import Any

from hermes_cli.config import get_hermes_home

PROJECT_ROOT = Path(__file__).parent.parent.resolve()

_DASHBOARD_PATTERNS = (
    "hermes dashboard",
    "hermes_cli.main dashboard",
    "hermes_cli/main.py dashboard",
)
_RUNTIME_DIRNAME = "dashboard"
_RUNTIME_FILENAME = "runtime.json"
_LOG_FILENAME = "dashboard.log"
_SECRET_ARG_MARKERS = (
    "api_key",
    "apikey",
    "auth",
    "client_secret",
    "credential",
    "key",
    "oauth",
    "pass",
    "password",
    "secret",
    "token",
)


@dataclass(frozen=True)
class DashboardLaunch:
    pid: int | None
    argv: list[str]
    cwd: str
    source: str


@dataclass(frozen=True)
class DashboardPortOwner:
    host: str
    port: int
    pid: int | None
    command_basename: str | None
    argv_summary: str | None
    is_hermes_dashboard: bool | None
    source: str


class DashboardPortInUse(RuntimeError):
    """Raised when the requested dashboard listener is already occupied."""


def dashboard_runtime_path() -> Path:
    return get_hermes_home() / _RUNTIME_DIRNAME / _RUNTIME_FILENAME


def dashboard_log_path() -> Path:
    return get_hermes_home() / "logs" / _LOG_FILENAME


def _looks_secret_arg(name: str) -> bool:
    normalized = name.strip().lstrip("-").lower().replace("-", "_")
    return any(marker in normalized for marker in _SECRET_ARG_MARKERS)


def _redact_argv(argv: list[str]) -> list[str]:
    redacted: list[str] = []
    redact_next = False
    for part in argv:
        if redact_next:
            redacted.append("<redacted>")
            redact_next = False
            continue
        if "=" in part:
            key, _value = part.split("=", 1)
            if _looks_secret_arg(key):
                redacted.append(f"{key}=<redacted>")
                continue
        if part.startswith("-") and _looks_secret_arg(part):
            redacted.append(part)
            redact_next = True
            continue
        redacted.append(part)
    return redacted


def safe_argv_summary(argv: list[str] | None, *, max_chars: int = 160) -> str | None:
    if not argv:
        return None
    summary = shlex.join(_redact_argv(argv))
    if len(summary) <= max_chars:
        return summary
    return summary[: max_chars - 3] + "..."


def _command_basename(argv: list[str] | None) -> str | None:
    if not argv:
        return None
    return Path(argv[0]).name or None


def _parse_proc_net_address(raw_address: str, *, ipv6: bool) -> str | None:
    raw_host, raw_port = raw_address.split(":", 1)
    try:
        if ipv6:
            packed = bytes.fromhex(raw_host)
            # /proc/net/tcp6 stores the 128-bit address as four little-endian words.
            packed = b"".join(packed[index : index + 4][::-1] for index in range(0, 16, 4))
            return str(ip_address(packed))
        return str(ip_address(bytes.fromhex(raw_host)[::-1]))
    except ValueError:
        return None


def _listening_socket_inodes(host: str, port: int) -> set[str]:
    if sys.platform == "win32":
        return set()
    try:
        requested = ip_address(host)
    except ValueError:
        return set()

    inodes: set[str] = set()
    for proc_net_path, ipv6 in ((Path("/proc/net/tcp"), False), (Path("/proc/net/tcp6"), True)):
        try:
            lines = proc_net_path.read_text(encoding="utf-8").splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            columns = line.split()
            if len(columns) < 10:
                continue
            local_address = columns[1]
            state = columns[3]
            inode = columns[9]
            if state != "0A":  # TCP_LISTEN
                continue
            try:
                raw_host, raw_port = local_address.split(":", 1)
                local_port = int(raw_port, 16)
            except ValueError:
                continue
            if local_port != port:
                continue
            parsed_host = _parse_proc_net_address(local_address, ipv6=ipv6)
            if parsed_host is None:
                continue
            try:
                local_ip = ip_address(parsed_host)
            except ValueError:
                continue
            if local_ip == requested or local_ip.is_unspecified or requested.is_unspecified:
                inodes.add(inode)
    return inodes


def detect_dashboard_port_owner(host: str, port: int) -> DashboardPortOwner | None:
    """Best-effort Linux owner lookup for an occupied dashboard listener."""
    inodes = _listening_socket_inodes(host, port)
    if not inodes:
        return None

    proc_root = Path("/proc")
    try:
        pid_dirs = [path for path in proc_root.iterdir() if path.name.isdigit()]
    except OSError:
        return DashboardPortOwner(host, port, None, None, None, None, "proc-net")

    for pid_dir in pid_dirs:
        try:
            pid = int(pid_dir.name)
        except ValueError:
            continue
        fd_dir = pid_dir / "fd"
        try:
            fd_paths = list(fd_dir.iterdir())
        except OSError:
            continue
        for fd_path in fd_paths:
            try:
                target = os.readlink(fd_path)
            except OSError:
                continue
            if not (target.startswith("socket:[") and target.endswith("]")):
                continue
            if target[len("socket:[") : -1] not in inodes:
                continue
            argv = _read_proc_argv(pid)
            command = " ".join(argv) if argv else ""
            return DashboardPortOwner(
                host=host,
                port=port,
                pid=pid,
                command_basename=_command_basename(argv),
                argv_summary=safe_argv_summary(argv),
                is_hermes_dashboard=_argv_matches_dashboard(argv) if argv else _command_matches_dashboard(command),
                source="proc-net-fd",
            )

    return DashboardPortOwner(host, port, None, None, None, None, "proc-net")


def dashboard_port_in_use_message(
    host: str,
    port: int,
    owner: DashboardPortOwner | None = None,
) -> str:
    lines = [f"Dashboard port {host}:{port} is already in use."]
    if owner is None:
        lines.append("Owner: unknown (listener owner could not be determined).")
    else:
        pid = str(owner.pid) if owner.pid is not None else "unknown"
        command = owner.command_basename or "unknown"
        summary = owner.argv_summary or "unknown"
        classification = (
            "Hermes dashboard"
            if owner.is_hermes_dashboard is True
            else "not Hermes dashboard"
            if owner.is_hermes_dashboard is False
            else "unknown"
        )
        lines.append(
            f"Owner: PID {pid}, command {command}, argv {summary}, classification: {classification}."
        )
    if owner is not None and owner.is_hermes_dashboard is True:
        lines.append(
            "Action: inspect with `hermes dashboard --status`; if the managed service should own "
            "the port, run `hermes dashboard --stop`, then `systemctl --user reset-failed "
            "hermes-dashboard.service` and `systemctl --user restart hermes-dashboard.service`."
        )
    else:
        lines.append(
            "Action: free the process using that port or start Hermes on another port with "
            "`hermes dashboard --port <port>`; do not auto-kill unmanaged processes. If "
            "hermes-dashboard.service is failed, run `systemctl --user reset-failed "
            "hermes-dashboard.service` after resolving the collision."
        )
    return " ".join(lines)


def ensure_dashboard_port_available(host: str, port: int) -> None:
    """Fail before uvicorn when the requested dashboard bind is unavailable."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            owner = detect_dashboard_port_owner(host, port)
            raise DashboardPortInUse(dashboard_port_in_use_message(host, port, owner)) from exc
        raise


def _restartable_argv(argv: list[str]) -> list[str]:
    """Return an argv suitable for subprocess.Popen."""
    if not argv:
        return []
    first = argv[0].lower()
    if first.endswith((".py", ".pyw")):
        return [sys.executable, *argv]
    return list(argv)


def _command_matches_dashboard(command: str) -> bool:
    return any(pattern in command for pattern in _DASHBOARD_PATTERNS)


def _argv_matches_dashboard(argv: list[str]) -> bool:
    """Return true only for a real dashboard process argv.

    Process-table scans can see wrapper shells whose ``bash -c`` payload
    mentions ``hermes dashboard --stop``. Matching only the command string can
    make lifecycle commands terminate their own wrapper instead of just the
    dashboard server. Keep the broad string match as a cheap prefilter, then
    validate the actual argv shape before returning a launch.
    """
    if not argv:
        return False

    def _name(part: str) -> str:
        return Path(part).name.lower()

    first = _name(argv[0])
    if first in {"hermes", "hermes.exe", "hermes.cmd", "hermes.bat"}:
        return len(argv) >= 2 and argv[1] == "dashboard"

    if "python" in first or first in {"py", "py.exe"}:
        if len(argv) >= 4 and argv[1] == "-m" and argv[2] == "hermes_cli.main":
            return argv[3] == "dashboard"
        if len(argv) >= 3 and _name(argv[1]) == "main.py":
            return argv[2] == "dashboard"

    if first == "main.py":
        return len(argv) >= 2 and argv[1] == "dashboard"

    return False


def _read_proc_argv(pid: int) -> list[str] | None:
    if sys.platform == "win32":
        return None
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    parts = [p.decode("utf-8", errors="replace") for p in raw.split(b"\x00") if p]
    return parts or None


def _read_proc_cwd(pid: int) -> str | None:
    if sys.platform == "win32":
        return None
    try:
        return str(Path(f"/proc/{pid}/cwd").resolve())
    except OSError:
        return None


def _pid_exists(pid: int) -> bool:
    try:
        from gateway.status import _pid_exists as _gateway_pid_exists

        return _gateway_pid_exists(pid)
    except Exception:
        try:
            os.kill(pid, 0)  # windows-footgun: fallback only when gateway helper is unavailable
            return True
        except Exception:
            return False


def _scan_dashboard_launches() -> list[DashboardLaunch]:
    self_pid = os.getpid()
    launches: list[DashboardLaunch] = []

    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["wmic", "process", "get", "ProcessId,CommandLine", "/FORMAT:LIST"],
                capture_output=True,
                text=True,
                timeout=10,
                encoding="utf-8",
                errors="ignore",
            )
            if result.returncode != 0 or result.stdout is None:
                return []
            current_cmd = ""
            for line in result.stdout.split("\n"):
                line = line.strip()
                if line.startswith("CommandLine="):
                    current_cmd = line[len("CommandLine=") :]
                elif line.startswith("ProcessId="):
                    pid_str = line[len("ProcessId=") :]
                    try:
                        pid = int(pid_str)
                    except ValueError:
                        continue
                    if pid == self_pid or not _command_matches_dashboard(current_cmd):
                        continue
                    argv = shlex.split(current_cmd, posix=False)
                    if not _argv_matches_dashboard(argv):
                        continue
                    launches.append(
                        DashboardLaunch(
                            pid=pid,
                            argv=argv,
                            cwd=str(PROJECT_ROOT),
                            source="process-table",
                        )
                    )
            return launches

        result = subprocess.run(
            ["ps", "-A", "-o", "pid=,command="],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return []
        for line in getattr(result, "stdout", "").split("\n"):
            stripped = line.strip()
            if not stripped or "grep" in stripped:
                continue
            parts = stripped.split(None, 1)
            if len(parts) != 2:
                continue
            try:
                pid = int(parts[0])
            except ValueError:
                continue
            command = parts[1]
            if pid == self_pid or not _command_matches_dashboard(command):
                continue
            argv = _read_proc_argv(pid)
            if argv is None:
                try:
                    argv = shlex.split(command)
                except ValueError:
                    argv = command.split()
            if not _argv_matches_dashboard(argv):
                continue
            launches.append(
                DashboardLaunch(
                    pid=pid,
                    argv=argv,
                    cwd=_read_proc_cwd(pid) or str(PROJECT_ROOT),
                    source="process-table",
                )
            )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []

    return launches


def find_dashboard_pids() -> list[int]:
    return [launch.pid for launch in _scan_dashboard_launches() if launch.pid is not None]


def record_dashboard_runtime(
    *,
    argv: list[str],
    cwd: str,
    host: str,
    port: int,
    skip_build: bool,
    tui: bool,
    insecure: bool,
) -> None:
    """Persist advisory launch metadata for later dashboard restarts."""
    runtime_path = dashboard_runtime_path()
    runtime_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": os.getpid(),
        "argv": _restartable_argv(argv),
        "cwd": cwd,
        "host": host,
        "port": port,
        "skip_build": bool(skip_build),
        "tui": bool(tui),
        "insecure": bool(insecure),
        "started_at": time.time(),
    }
    tmp = runtime_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(runtime_path)


def _runtime_launch() -> DashboardLaunch | None:
    runtime_path = dashboard_runtime_path()
    try:
        data = json.loads(runtime_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    pid = data.get("pid")
    argv = data.get("argv")
    cwd = data.get("cwd")
    if not isinstance(pid, int) or not isinstance(argv, list) or not argv:
        return None
    if not all(isinstance(part, str) and part for part in argv):
        return None
    if not isinstance(cwd, str) or not cwd:
        cwd = str(PROJECT_ROOT)
    if not _pid_exists(pid):
        return None
    proc_argv = _read_proc_argv(pid)
    proc_command = " ".join(proc_argv) if proc_argv else " ".join(argv)
    if not _command_matches_dashboard(proc_command):
        return None
    return DashboardLaunch(pid=pid, argv=list(argv), cwd=cwd, source="runtime")


def _best_restart_launch() -> DashboardLaunch | None:
    runtime = _runtime_launch()
    if runtime is not None:
        return runtime
    launches = _scan_dashboard_launches()
    return launches[0] if launches else None


def stop_dashboard_processes(
    *,
    reason: str = "requested via dashboard lifecycle",
    print_restart_hint: bool = True,
) -> bool:
    """Stop running dashboard processes. Returns true when all found PIDs stopped."""
    pids = find_dashboard_pids()
    if not pids:
        return False

    print()
    print(f"⟲ Stopping {len(pids)} dashboard process(es) ({reason})")

    killed: list[int] = []
    failed: list[tuple[int, str]] = []

    if sys.platform == "win32":
        for pid in pids:
            try:
                result = subprocess.run(
                    ["taskkill", "/PID", str(pid), "/F"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if result.returncode == 0:
                    killed.append(pid)
                else:
                    failed.append((pid, (result.stderr or result.stdout or "").strip()))
            except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
                failed.append((pid, str(e)))
    else:
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                killed.append(pid)
            except (PermissionError, OSError) as e:
                failed.append((pid, str(e)))

        deadline = time.monotonic() + 3.0
        pending = [p for p in pids if p not in killed and p not in {f[0] for f in failed}]
        while pending and time.monotonic() < deadline:
            time.sleep(0.1)
            still_pending = []
            for pid in pending:
                if _pid_exists(pid):
                    still_pending.append(pid)
                else:
                    killed.append(pid)
            pending = still_pending

        for pid in pending:
            try:
                os.kill(pid, signal.SIGKILL)
                killed.append(pid)
            except ProcessLookupError:
                killed.append(pid)
            except (PermissionError, OSError) as e:
                failed.append((pid, str(e)))

    for pid in killed:
        print(f"    ✓ stopped PID {pid}")
    for pid, err_msg in failed:
        print(f"    ✗ failed to stop PID {pid}: {err_msg}")

    if killed and print_restart_hint:
        print("  Restart the dashboard when you're ready:")
        print("    hermes dashboard --port <port>")
    return bool(killed) and not failed


def _spawn_dashboard(launch: DashboardLaunch) -> subprocess.Popen[Any]:
    log_path = dashboard_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab", buffering=0) as log_file:
        log_file.write(
            f"\n=== dashboard restarted {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n".encode()
        )
        popen_kwargs: dict[str, Any] = {
            "cwd": launch.cwd,
            "stdin": subprocess.DEVNULL,
            "stdout": log_file,
            "stderr": subprocess.STDOUT,
            "env": os.environ.copy(),
        }
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
                | getattr(subprocess, "DETACHED_PROCESS", 0)
            )
        else:
            popen_kwargs["start_new_session"] = True
        return subprocess.Popen(_restartable_argv(launch.argv), **popen_kwargs)


def restart_dashboard_if_running(
    *, reason: str = "dashboard restart", quiet_when_missing: bool = False
) -> bool:
    """Restart the dashboard only when a running dashboard can be identified."""
    launch = _best_restart_launch()
    if launch is None:
        if not quiet_when_missing:
            print("No hermes dashboard processes running.")
        return False

    if not stop_dashboard_processes(reason=reason, print_restart_hint=False):
        print("Dashboard restart aborted because the running process could not be stopped.")
        return False
    proc = _spawn_dashboard(launch)
    print(f"✓ Restarted dashboard (PID {proc.pid})")
    return True
