"""Best-effort gateway and isolated child memory telemetry."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence


GATEWAY_CHILD_UNIT_PREFIX = "hermes-gateway-child-"
_SECRET_RE = re.compile(r"(?i)(api[_-]?key|token|secret|password|passwd|authorization|bearer)(=|:)[^\s]+")


@dataclass(frozen=True)
class ProcessMemory:
    pid: int
    ppid: int = 0
    rss_kb: int = 0
    name: str = ""
    command: str = ""


@dataclass(frozen=True)
class ChildMemory:
    pid: int
    rss_kb: int
    kind: str
    label: str
    unit: str = ""
    source: str = "proc"


@dataclass(frozen=True)
class GatewayMemoryTelemetry:
    gateway_pids: tuple[int, ...]
    gateway_rss_kb: int
    child_rss_kb: int
    top_children: tuple[ChildMemory, ...]
    source: str
    warnings: tuple[str, ...] = ()


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _proc_pid_dir(proc_root: Path, pid: int) -> Path:
    return proc_root / str(pid)


def _parse_status(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        out[key.strip()] = value.strip()
    return out


def _parse_rss_kb(value: str) -> int:
    match = re.search(r"(\d+)", value or "")
    return int(match.group(1)) if match else 0


def _read_cmdline(pid_dir: Path) -> str:
    try:
        raw = (pid_dir / "cmdline").read_bytes()
    except (FileNotFoundError, PermissionError, OSError):
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()


def _read_cgroup(pid_dir: Path) -> str:
    try:
        return _read_text(pid_dir / "cgroup")
    except (FileNotFoundError, PermissionError, OSError, UnicodeError):
        return ""


def read_process_memory(pid: int, *, proc_root: Path = Path("/proc")) -> ProcessMemory | None:
    pid_dir = _proc_pid_dir(proc_root, pid)
    try:
        status = _parse_status(_read_text(pid_dir / "status"))
    except (FileNotFoundError, PermissionError, OSError, UnicodeError):
        return None
    try:
        parsed_pid = int(status.get("Pid", pid))
    except (TypeError, ValueError):
        parsed_pid = pid
    try:
        ppid = int(status.get("PPid", "0") or "0")
    except (TypeError, ValueError):
        ppid = 0
    rss_kb = _parse_rss_kb(status.get("VmRSS", ""))
    return ProcessMemory(
        pid=parsed_pid,
        ppid=ppid,
        rss_kb=rss_kb,
        name=status.get("Name", ""),
        command=_read_cmdline(pid_dir),
    )


def _iter_proc_processes(proc_root: Path) -> Iterable[ProcessMemory]:
    try:
        entries = list(proc_root.iterdir())
    except (FileNotFoundError, PermissionError, OSError):
        return []
    processes: list[ProcessMemory] = []
    for entry in entries:
        if not entry.name.isdigit():
            continue
        proc = read_process_memory(int(entry.name), proc_root=proc_root)
        if proc is not None:
            processes.append(proc)
    return processes


def _descendant_pids(processes: Sequence[ProcessMemory], roots: set[int]) -> set[int]:
    children_by_parent: dict[int, list[int]] = {}
    for proc in processes:
        children_by_parent.setdefault(proc.ppid, []).append(proc.pid)
    found: set[int] = set()
    stack = list(roots)
    while stack:
        parent = stack.pop()
        for child in children_by_parent.get(parent, []):
            if child in roots or child in found:
                continue
            found.add(child)
            stack.append(child)
    return found


def _tree_rss_kb(processes_by_pid: dict[int, ProcessMemory], root_pid: int) -> int:
    roots = {root_pid}
    descendants = _descendant_pids(tuple(processes_by_pid.values()), roots)
    return sum(
        proc.rss_kb
        for pid in roots | descendants
        if (proc := processes_by_pid.get(pid)) is not None
    )


def sanitize_process_label(label: str, *, max_len: int = 96) -> str:
    cleaned = _SECRET_RE.sub(r"\1\2[redacted]", " ".join(str(label or "").split()))
    if len(cleaned) > max_len:
        return cleaned[: max_len - 3].rstrip() + "..."
    return cleaned


def _label_for_process(proc: ProcessMemory) -> str:
    return sanitize_process_label(proc.command or proc.name or f"pid {proc.pid}")


def _kind_from_unit(unit: str) -> str:
    name = unit.removesuffix(".scope")
    if not name.startswith(GATEWAY_CHILD_UNIT_PREFIX):
        return "child"
    rest = name[len(GATEWAY_CHILD_UNIT_PREFIX) :]
    return (rest.split("-", 1)[0] or "child")[:40]


def _parse_systemctl_show(stdout: str) -> dict[str, str]:
    props: dict[str, str] = {}
    for line in stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        props[key] = value.strip()
    return props


def _run_systemctl(args: Sequence[str], *, timeout: float = 2.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", "--user", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _systemd_child_units(
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = _run_systemctl,
) -> tuple[list[tuple[str, int, str, str]], list[str]]:
    warnings: list[str] = []
    try:
        result = runner(
            ["list-units", f"{GATEWAY_CHILD_UNIT_PREFIX}*.scope", "--plain", "--no-legend", "--no-pager"],
            timeout=2.0,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        return [], [f"systemd child scan unavailable: {exc.__class__.__name__}"]
    if result.returncode != 0:
        return [], []

    units: list[tuple[str, int, str, str]] = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if not parts or not parts[0].startswith(GATEWAY_CHILD_UNIT_PREFIX):
            continue
        unit = parts[0]
        try:
            show = runner(
                [
                    "show",
                    unit,
                    "--no-pager",
                    "--property",
                    "MainPID",
                    "--property",
                    "Description",
                    "--property",
                    "ControlGroup",
                ],
                timeout=2.0,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            warnings.append(f"systemd show failed for {unit}: {exc.__class__.__name__}")
            continue
        if show.returncode != 0:
            continue
        props = _parse_systemctl_show(show.stdout)
        try:
            pid = int(props.get("MainPID", "0") or "0")
        except (TypeError, ValueError):
            pid = 0
        if pid > 0 or props.get("ControlGroup"):
            units.append((unit, pid, props.get("Description", ""), props.get("ControlGroup", "")))
    return units, warnings


def _pids_in_cgroup(processes: Sequence[ProcessMemory], proc_root: Path, cgroup: str) -> list[int]:
    cgroup = str(cgroup or "").strip()
    if not cgroup:
        return []
    pids: list[int] = []
    for proc in processes:
        proc_cgroup = _read_cgroup(_proc_pid_dir(proc_root, proc.pid))
        if cgroup in proc_cgroup:
            pids.append(proc.pid)
    return pids


def collect_gateway_memory_telemetry(
    gateway_pids: Sequence[int],
    *,
    top_n: int = 5,
    proc_root: Path = Path("/proc"),
    systemctl_runner: Callable[..., subprocess.CompletedProcess[str]] = _run_systemctl,
) -> GatewayMemoryTelemetry:
    roots = {int(pid) for pid in gateway_pids if int(pid) > 0}
    processes = list(_iter_proc_processes(proc_root))
    by_pid = {proc.pid: proc for proc in processes}
    gateway_rss = sum((by_pid.get(pid).rss_kb if by_pid.get(pid) else 0) for pid in roots)
    warnings: list[str] = []
    children: dict[tuple[str, int], ChildMemory] = {}

    units, systemd_warnings = _systemd_child_units(runner=systemctl_runner)
    warnings.extend(systemd_warnings)
    for unit, pid, description, cgroup in units:
        if pid <= 0:
            cgroup_pids = _pids_in_cgroup(processes, proc_root, cgroup)
            pid = min(cgroup_pids) if cgroup_pids else 0
        proc = by_pid.get(pid) or (read_process_memory(pid, proc_root=proc_root) if pid > 0 else None)
        if proc is None:
            continue
        label = description.removeprefix("Hermes gateway child ").strip(": ") or _label_for_process(proc)
        children[("systemd", pid)] = ChildMemory(
            pid=pid,
            rss_kb=_tree_rss_kb(by_pid, pid),
            kind=_kind_from_unit(unit),
            label=sanitize_process_label(label),
            unit=unit,
            source="systemd",
        )

    descendant_ids = _descendant_pids(processes, roots)
    for pid in descendant_ids:
        if any(existing.pid == pid for existing in children.values()):
            continue
        proc = by_pid.get(pid)
        if proc is None:
            continue
        children[("proc", pid)] = ChildMemory(
            pid=pid,
            rss_kb=proc.rss_kb,
            kind="proc-child",
            label=_label_for_process(proc),
            source="proc",
        )

    ordered = sorted(children.values(), key=lambda child: child.rss_kb, reverse=True)
    child_total = sum(child.rss_kb for child in children.values())
    source = "systemd+/proc" if units else "/proc"
    return GatewayMemoryTelemetry(
        gateway_pids=tuple(sorted(roots)),
        gateway_rss_kb=gateway_rss,
        child_rss_kb=child_total,
        top_children=tuple(ordered[: max(top_n, 0)]),
        source=source,
        warnings=tuple(warnings),
    )


def format_kb(kb: int) -> str:
    if kb >= 1024 * 1024:
        return f"{kb / (1024 * 1024):.1f} GiB"
    if kb >= 1024:
        return f"{kb / 1024:.1f} MiB"
    return f"{kb} KiB"


def format_gateway_memory_lines(telemetry: GatewayMemoryTelemetry) -> list[str]:
    lines = [
        f"Gateway RSS: {format_kb(telemetry.gateway_rss_kb)}",
        f"Isolated child/helper RSS: {format_kb(telemetry.child_rss_kb)}",
    ]
    if telemetry.top_children:
        lines.append("Top child/helper RSS:")
        for child in telemetry.top_children:
            unit = f" unit={child.unit}" if child.unit else ""
            lines.append(
                f"  PID {child.pid} {child.kind}{unit}: {format_kb(child.rss_kb)} - {child.label}"
            )
    else:
        lines.append("Top child/helper RSS: none detected")
    return lines
