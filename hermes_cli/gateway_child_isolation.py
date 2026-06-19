"""Best-effort systemd scopes for gateway-launched child processes."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence


_GATEWAY_CHILD_ENV_EXACT = frozenset(
    {
        "CODEX_HOME",
        "CLOUDSDK_CONFIG",
        "DOCKER_CONFIG",
        "GH_CONFIG_DIR",
        "GIT_ASKPASS",
        "GIT_AUTHOR_EMAIL",
        "GIT_AUTHOR_NAME",
        "GIT_COMMITTER_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_CONFIG_GLOBAL",
        "GIT_SSH_COMMAND",
        "HOME",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "HERMES_HOME",
        "HERMES_PROFILE",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "NO_PROXY",
        "PATH",
        "PYTHONPATH",
        "REQUESTS_CA_BUNDLE",
        "RUST_LOG",
        "SHELL",
        "SSL_CERT_FILE",
        "SSH_ASKPASS",
        "SSH_AUTH_SOCK",
        "TERM",
        "TMPDIR",
        "USER",
        "VIRTUAL_ENV",
        "XDG_CONFIG_HOME",
        "XDG_RUNTIME_DIR",
    }
)

_GATEWAY_CHILD_ENV_PREFIXES = (
    "HERMES_CODEX_WORKER_",
    "HERMES_KANBAN_",
    "HERMES_SESSION_",
    "HERMES_PROJECT_",
)


@dataclass(frozen=True)
class GatewayChildScope:
    """Metadata for a best-effort gateway child systemd scope."""

    enabled: bool
    unit: str = ""
    kind: str = ""
    purpose: str = ""
    command_label: str = ""
    workspace: str = ""
    session_key: str = ""


def gateway_child_systemd_env(env: Mapping[str, str]) -> dict[str, str]:
    """Return env keys safe to expose as transient systemd unit properties."""
    out: dict[str, str] = {}
    for key, value in (env or {}).items():
        if not value:
            continue
        if key in _GATEWAY_CHILD_ENV_EXACT or key.startswith(_GATEWAY_CHILD_ENV_PREFIXES):
            out[key] = str(value)
    return out


def should_use_gateway_child_scope() -> bool:
    """Whether gateway child processes can be launched in user systemd scopes."""
    override = os.environ.get("HERMES_GATEWAY_CHILD_SYSTEMD", "").strip().lower()
    if override in {"0", "false", "no", "off"}:
        return False
    if os.name == "nt" or sys.platform == "win32":
        return False
    return bool(shutil.which("systemd-run") and shutil.which("systemctl"))


def _safe_unit_part(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "")).strip(".-")
    return cleaned or fallback


def gateway_child_scope_name(
    *,
    kind: str,
    session_key: str = "",
    command_label: str = "",
) -> str:
    """Return a safe transient scope name with a discoverable Hermes prefix."""
    kind_part = _safe_unit_part(kind, "child")[:40]
    session_part = _safe_unit_part(session_key, "nosession")[:64]
    label_part = _safe_unit_part(command_label, "cmd")[:40]
    millis = int(time.time() * 1000)
    raw = f"hermes-gateway-child-{kind_part}-{session_part}-{label_part}-{millis}"
    return raw[:240].strip(".-") or "hermes-gateway-child"


def build_gateway_child_scope_argv(
    command: Sequence[str],
    *,
    env: Optional[Mapping[str, str]] = None,
    cwd: str = "",
    kind: str,
    purpose: str,
    command_label: str = "",
    session_key: str = "",
    pipe_stdio: bool = True,
) -> tuple[list[str], GatewayChildScope]:
    """Wrap *command* in ``systemd-run --user --scope`` when supported.

    The returned argv is safe to pass to ``subprocess.Popen`` or SDK launch
    helpers. When user systemd is unavailable, argv is returned unchanged and
    metadata has ``enabled=False``.
    """
    base = [str(part) for part in command]
    if not should_use_gateway_child_scope():
        return base, GatewayChildScope(
            enabled=False,
            kind=kind,
            purpose=purpose,
            command_label=command_label,
            workspace=cwd,
            session_key=session_key,
        )

    systemd_run = shutil.which("systemd-run") or "systemd-run"
    unit = gateway_child_scope_name(
        kind=kind,
        session_key=session_key,
        command_label=command_label,
    )
    args = [
        systemd_run,
        "--user",
        "--scope",
        "--unit",
        unit,
        "--collect",
        "--quiet",
    ]
    # systemd-run rejects --pipe with --scope on common systemd versions. Scope
    # units inherit this wrapper's stdio, so callers can still attach pipes to
    # systemd-run and keep JSON-RPC/stdout behavior intact.
    if cwd and os.path.isdir(cwd):
        args.extend(["--working-directory", cwd])
    description = f"Hermes gateway child {kind}: {purpose}"[:200]
    args.extend(["--property", f"Description={description}"])
    for key, value in sorted(gateway_child_systemd_env(env or {}).items()):
        args.append(f"--setenv={key}={value}")
    args.extend(["--", *base])
    return args, GatewayChildScope(
        enabled=True,
        unit=f"{unit}.scope",
        kind=kind,
        purpose=purpose,
        command_label=command_label,
        workspace=cwd,
        session_key=session_key,
    )
