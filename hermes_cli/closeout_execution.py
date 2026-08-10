"""Lease-aware command classification and process containment for closeout."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from agent.execution_guard import ExecutionGuardExpired
from hermes_cli.github_remote import github_cli_env


class CommandEffect(str, Enum):
    READ_ONLY = "read_only"
    LOCAL_MUTATION = "local_mutation"
    REMOTE_MUTATION = "remote_mutation"


@dataclass(frozen=True)
class ClassifiedCommand:
    effect: CommandEffect
    operation: str


class UnsupportedCloseoutCommand(RuntimeError):
    """Raised before an unrecognized closeout command can execute."""


class RemoteMutationUncertain(TimeoutError):
    """Raised after a remote mutation is terminated without a known outcome."""

    def __init__(self, operation: str, reason: str) -> None:
        self.operation = str(operation or "remote_mutation")[:80]
        self.reason = str(reason or "outcome unknown")[:160]
        super().__init__(f"{self.operation} outcome is uncertain")


_GIT_READ_ONLY = frozenset(
    {
        "cat-file",
        "ls-remote",
        "merge-base",
        "rev-parse",
        "status",
        "symbolic-ref",
    }
)
_GIT_LOCAL_MUTATIONS = frozenset({"checkout", "fetch", "merge"})
_GIT_REMOTE_MUTATIONS = frozenset({"push"})
_GH_PR_READ_ONLY = frozenset({"list", "view"})
_GH_PR_MUTATIONS = frozenset({"create", "merge", "ready"})
_GH_RUN_READ_ONLY = frozenset({"list", "view"})
_GH_API_MUTATION_FLAGS = frozenset(
    {
        "--field",
        "--input",
        "--method",
        "--raw-field",
        "-F",
        "-f",
        "-X",
    }
)


def _tokens(args: Sequence[Any]) -> list[str]:
    return [str(value or "").strip() for value in args]


def classify_closeout_command(args: Sequence[Any]) -> ClassifiedCommand:
    """Classify one allowlisted command, rejecting unknown Git/GitHub verbs."""

    tokens = _tokens(args)
    executable = Path(tokens[0]).name.lower() if tokens else ""
    if executable == "git":
        if len(tokens) < 2 or tokens[1].startswith("-"):
            raise UnsupportedCloseoutCommand("unsupported Git command")
        verb = tokens[1].lower()
        if verb in _GIT_READ_ONLY:
            return ClassifiedCommand(CommandEffect.READ_ONLY, f"git_{verb.replace('-', '_')}")
        if verb == "worktree" and len(tokens) >= 3 and tokens[2].lower() == "list":
            return ClassifiedCommand(CommandEffect.READ_ONLY, "git_worktree_list")
        if verb in _GIT_LOCAL_MUTATIONS:
            return ClassifiedCommand(
                CommandEffect.LOCAL_MUTATION,
                f"git_{verb.replace('-', '_')}",
            )
        if verb in _GIT_REMOTE_MUTATIONS:
            return ClassifiedCommand(
                CommandEffect.REMOTE_MUTATION,
                f"git_{verb.replace('-', '_')}",
            )
        raise UnsupportedCloseoutCommand("unsupported Git command")

    if executable == "gh":
        if len(tokens) < 3:
            raise UnsupportedCloseoutCommand("unsupported GitHub CLI command")
        group = tokens[1].lower()
        verb = tokens[2].lower()
        if group == "auth" and verb == "status":
            return ClassifiedCommand(CommandEffect.READ_ONLY, "github_auth_status")
        if group == "pr" and verb in _GH_PR_READ_ONLY:
            return ClassifiedCommand(CommandEffect.READ_ONLY, f"github_pr_{verb}")
        if group == "pr" and verb in _GH_PR_MUTATIONS:
            return ClassifiedCommand(CommandEffect.REMOTE_MUTATION, f"github_pr_{verb}")
        if group == "run" and verb in _GH_RUN_READ_ONLY:
            return ClassifiedCommand(CommandEffect.READ_ONLY, f"github_run_{verb}")
        if group == "api":
            for index, token in enumerate(tokens[2:], start=2):
                flag = token.split("=", 1)[0]
                if flag not in _GH_API_MUTATION_FLAGS:
                    continue
                if flag in {"--method", "-X"}:
                    method = (
                        token.split("=", 1)[1].upper()
                        if "=" in token
                        else tokens[index + 1].upper()
                        if index + 1 < len(tokens)
                        else ""
                    )
                    if method == "GET":
                        continue
                raise UnsupportedCloseoutCommand("unsupported GitHub API mutation")
            return ClassifiedCommand(CommandEffect.READ_ONLY, "github_api_get")
        raise UnsupportedCloseoutCommand("unsupported GitHub CLI command")

    if executable == "vercel":
        if (
            len(tokens) == 4
            and tokens[1].lower() == "inspect"
            and tokens[3] == "--json"
        ):
            parsed = urlsplit(tokens[2])
            hostname = str(parsed.hostname or "").lower()
            if (
                parsed.scheme == "https"
                and hostname.endswith(".vercel.app")
                and not parsed.username
                and not parsed.password
                and parsed.path in {"", "/"}
                and not parsed.query
                and not parsed.fragment
            ):
                return ClassifiedCommand(CommandEffect.READ_ONLY, "vercel_inspect")
        raise UnsupportedCloseoutCommand("unsupported Vercel CLI command")

    raise UnsupportedCloseoutCommand(
        "closeout commands must use Git, GitHub CLI, or bounded Vercel inspection"
    )


def _control_remaining(control: Any | None, requested: float) -> float:
    timeout = max(0.001, float(requested))
    remaining = getattr(control, "remaining", None)
    if not callable(remaining):
        return timeout
    try:
        return max(0.001, min(timeout, float(remaining())))
    except Exception:
        return 0.001


def _control_allows_mutation(control: Any | None) -> bool:
    if control is None:
        return True
    mutation_allowed = getattr(control, "mutation_allowed", None)
    if callable(mutation_allowed):
        try:
            return bool(mutation_allowed())
        except Exception:
            return False
    cancelled = getattr(control, "cancelled", None)
    if callable(cancelled):
        try:
            return not bool(cancelled())
        except Exception:
            return False
    return not bool(cancelled)


def _control_reason(control: Any | None) -> str:
    reason = getattr(control, "reason", "")
    return str(reason or "execution cancelled")[:160]


def _signal_process_group(process: subprocess.Popen[str], sig: int) -> None:
    if os.name == "posix" and hasattr(os, "killpg"):
        os.killpg(process.pid, sig)
        return
    if sig == getattr(signal, "SIGTERM", None):
        process.terminate()
    else:
        process.kill()


def _terminate_process_group(process: subprocess.Popen[str]) -> tuple[str, str]:
    """Terminate the entire process group, escalate, and reap its leader."""

    try:
        _signal_process_group(process, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    graceful_deadline = time.monotonic() + 0.1
    while process.poll() is None and time.monotonic() < graceful_deadline:
        time.sleep(0.005)
    try:
        # Kill the group even when its leader exited after SIGTERM; descendants
        # may still be alive in the independently created session.
        _signal_process_group(process, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        if process.poll() is None:
            try:
                process.kill()
            except Exception:
                pass
    try:
        stdout, stderr = process.communicate(timeout=0.5)
        return stdout or "", stderr or ""
    except subprocess.TimeoutExpired as exc:
        stdout = exc.output or ""
        stderr = exc.stderr or ""

    # A descendant may have inherited the pipes and kept them open even after
    # the process-group kill. Stop waiting for EOF, close our pipe endpoints,
    # and reap only within a fixed deadline.
    for pipe in (process.stdout, process.stderr):
        if pipe is not None:
            try:
                pipe.close()
            except OSError:
                pass
    reap_deadline = time.monotonic() + 1.0
    while process.poll() is None and time.monotonic() < reap_deadline:
        try:
            process.wait(timeout=min(0.05, reap_deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            try:
                _signal_process_group(process, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
    if process.poll() is None:
        raise RuntimeError("closeout process group could not be reaped")
    return str(stdout or ""), str(stderr or "")


def run_closeout_command(
    args: list[str],
    *,
    cwd: Path,
    timeout: int | float = 60,
    github: bool = False,
    control: Any | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one allowlisted command with killable containment for mutations."""

    classified = classify_closeout_command(args)
    command_env = dict(env) if env is not None else github_cli_env() if github else None
    bounded_timeout = _control_remaining(control, float(timeout))
    if classified.effect == CommandEffect.READ_ONLY:
        return subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=bounded_timeout,
            check=False,
            env=command_env,
        )

    if os.name != "posix" or not hasattr(os, "killpg"):
        raise UnsupportedCloseoutCommand(
            "mutation containment requires POSIX process groups"
        )
    if not _control_allows_mutation(control):
        if classified.effect == CommandEffect.REMOTE_MUTATION:
            raise RemoteMutationUncertain(classified.operation, _control_reason(control))
        raise ExecutionGuardExpired(_control_reason(control))

    process = subprocess.Popen(
        args,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=command_env,
        start_new_session=True,
    )
    deadline = time.monotonic() + bounded_timeout
    while True:
        if not _control_allows_mutation(control):
            _terminate_process_group(process)
            if classified.effect == CommandEffect.REMOTE_MUTATION:
                raise RemoteMutationUncertain(
                    classified.operation,
                    _control_reason(control),
                )
            raise ExecutionGuardExpired(_control_reason(control))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _terminate_process_group(process)
            if classified.effect == CommandEffect.REMOTE_MUTATION:
                raise RemoteMutationUncertain(classified.operation, "command timeout")
            raise subprocess.TimeoutExpired(args, bounded_timeout)
        try:
            stdout, stderr = process.communicate(timeout=min(0.05, remaining))
            return subprocess.CompletedProcess(
                args,
                int(process.returncode or 0),
                stdout or "",
                stderr or "",
            )
        except subprocess.TimeoutExpired:
            continue


__all__ = [
    "ClassifiedCommand",
    "CommandEffect",
    "RemoteMutationUncertain",
    "UnsupportedCloseoutCommand",
    "classify_closeout_command",
    "run_closeout_command",
]
