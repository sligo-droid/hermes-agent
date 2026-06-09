"""Shared gateway restart constants, parsing, and watcher helpers."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Iterable

from hermes_cli.config import DEFAULT_CONFIG

# EX_TEMPFAIL from sysexits.h — used to ask the service manager to restart
# the gateway after a graceful drain/reload path completes.
GATEWAY_SERVICE_RESTART_EXIT_CODE = 75

DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT = float(
    DEFAULT_CONFIG["agent"]["restart_drain_timeout"]
)


def parse_restart_drain_timeout(raw: object) -> float:
    """Parse a configured drain timeout, falling back to the shared default."""
    try:
        value = float(raw) if str(raw or "").strip() else DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT
    except (TypeError, ValueError):
        return DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT
    return max(0.0, value)


@dataclass(frozen=True)
class RestartProcessInfo:
    pid: int
    ppid: int | None = None
    pgid: int | None = None
    sid: int | None = None
    cmdline: str = ""


@dataclass(frozen=True)
class RestartBlockerEvidence:
    active_agents: int = 0
    blockers: tuple[RestartProcessInfo, ...] = ()

    def blocker_summary(self) -> str:
        if not self.blockers:
            return "none"
        return "; ".join(
            f"pid={proc.pid} cmd={proc.cmdline or '(unknown)'}"
            for proc in self.blockers
        )


def _read_process_info(pid: int) -> RestartProcessInfo | None:
    try:
        import psutil  # type: ignore

        proc = psutil.Process(int(pid))
        try:
            cmdline = " ".join(proc.cmdline())
        except Exception:
            cmdline = proc.name() or ""
        try:
            pgid = os.getpgid(proc.pid) if hasattr(os, "getpgid") else None
        except OSError:
            pgid = None
        try:
            sid = os.getsid(proc.pid) if hasattr(os, "getsid") else None
        except OSError:
            sid = None
        return RestartProcessInfo(
            pid=proc.pid,
            ppid=proc.ppid() or None,
            pgid=pgid,
            sid=sid,
            cmdline=cmdline,
        )
    except Exception:
        return None


def _direct_children(root_pid: int) -> tuple[RestartProcessInfo, ...]:
    try:
        import psutil  # type: ignore

        parent = psutil.Process(int(root_pid))
        children = parent.children(recursive=False)
    except Exception:
        return ()

    infos: list[RestartProcessInfo] = []
    for child in children:
        info = _read_process_info(child.pid)
        if info is not None:
            infos.append(info)
    return tuple(infos)


def _current_process_identity(self_pid: int | None = None) -> tuple[int, int | None, int | None]:
    pid = int(self_pid or os.getpid())
    try:
        pgid = os.getpgid(pid) if hasattr(os, "getpgid") else None
    except OSError:
        pgid = None
    try:
        sid = os.getsid(pid) if hasattr(os, "getsid") else None
    except OSError:
        sid = None
    return pid, pgid, sid


def is_restart_watcher_self_process(
    proc: RestartProcessInfo,
    *,
    self_pid: int | None = None,
) -> bool:
    """Return True for the restart watcher wrapper/current process lineage.

    Detached restart wrappers are launched in a fresh process group/session and
    may remain direct children of the old gateway until they exec
    ``hermes gateway restart``.  They must not be classified as restart-sensitive
    children, while unrelated direct children of the old gateway still block.
    """
    current_pid, current_pgid, current_sid = _current_process_identity(self_pid)
    if proc.pid == current_pid:
        return True
    if current_pgid is not None and proc.pgid == current_pgid:
        return True
    if current_sid is not None and proc.sid == current_sid:
        return True
    return False


def restart_blocker_evidence(
    previous_pid: int | None,
    *,
    runtime_state: dict[str, Any] | None = None,
    direct_children: Iterable[RestartProcessInfo] | None = None,
    self_pid: int | None = None,
) -> RestartBlockerEvidence:
    """Return active-agent and direct-child evidence that can defer restart.

    ``direct_children`` is injectable so tests can cover process-classifier edge
    cases without depending on the host process table.
    """
    active_agents = 0
    if runtime_state:
        try:
            active_agents = max(0, int(runtime_state.get("active_agents", 0) or 0))
        except (TypeError, ValueError):
            active_agents = 0

    children = tuple(direct_children or (_direct_children(previous_pid) if previous_pid else ()))
    blockers = tuple(
        child
        for child in children
        if child.pid > 0 and not is_restart_watcher_self_process(child, self_pid=self_pid)
    )
    return RestartBlockerEvidence(active_agents=active_agents, blockers=blockers)
