"""Disposable, network-isolated verification for read-only runtimes."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from agent.runtime_capabilities import RuntimeMode, ToolEffect, normalize_runtime_mode
from gateway.session_context import get_session_env
from tools.registry import registry, tool_error


_SHELL_CONTROL_CHARS = frozenset("\n\r;|&<>`")
_SAFE_SCRIPT_NAMES = re.compile(
    r"^(?:test|tests|lint|type[-_]?check|check|verify|verification|build)(?::[\w.-]+)?$",
    re.IGNORECASE,
)
_SAFE_MAKE_TARGET = re.compile(
    r"^(?:test|tests|lint|type[-_]?check|check|verify|verification|build)(?:[-_:][\w.-]+)?$",
    re.IGNORECASE,
)
_OUTPUT_LIMIT = 100_000
_SENSITIVE_ENV_NAME = re.compile(
    r"(?:^|_)(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|ACCESS_KEY|PRIVATE_KEY|"
    r"CREDENTIALS?|AUTH(?:ORIZATION)?|COOKIE|PAT|DSN)(?:_|$)"
)
_SENSITIVE_ENV_SUFFIXES = (
    "_KEY",
    "_URL",
    "_URI",
    "_DSN",
    "_CONNECTION_STRING",
)
_SENSITIVE_ENV_EXACT = frozenset(
    {
        "DATABASE_URL",
        "DOCKER_AUTH_CONFIG",
        "KUBECONFIG",
        "NETRC",
    }
)


def check_read_only_verification_requirements() -> bool:
    return bool(shutil.which("git") and shutil.which("bwrap"))


def _path_arg_is_safe(arg: str) -> bool:
    if not arg or arg == "-":
        return True
    if os.path.isabs(arg):
        return False
    value = arg.split("=", 1)[-1] if "=" in arg else arg
    if os.path.isabs(value):
        return False
    return ".." not in PurePosixPath(value.replace("\\", "/")).parts


def parse_read_only_verification_command(command: Any) -> tuple[list[str] | None, str | None]:
    """Parse a deliberately small set of test/check commands without a shell."""

    if not isinstance(command, str) or not command.strip():
        return None, "command must be a non-empty string"
    if any(char in command for char in _SHELL_CONTROL_CHARS) or "$(" in command:
        return None, "shell control operators, substitutions, and redirections are not allowed"
    try:
        argv = shlex.split(command, posix=True)
    except ValueError as exc:
        return None, f"command could not be parsed safely: {exc}"
    if not argv:
        return None, "command must contain an executable"
    if any("=" in arg and index == 0 for index, arg in enumerate(argv)):
        return None, "environment assignments are not allowed"
    if any(not _path_arg_is_safe(arg) for arg in argv[1:]):
        return None, "absolute paths and parent-directory traversal are not allowed"

    executable = argv[0].removeprefix("./")
    base = Path(executable).name.lower()
    allowed = False
    if executable == "scripts/run_tests.sh":
        allowed = True
    elif base in {"pytest", "py.test"}:
        allowed = True
    elif base in {"python", "python3"}:
        allowed = len(argv) >= 3 and argv[1:3] == ["-m", "pytest"]
    elif base in {"npm", "pnpm", "yarn", "bun"}:
        tail = argv[1:]
        if tail and tail[0] == "run":
            tail = tail[1:]
        allowed = bool(tail and _SAFE_SCRIPT_NAMES.fullmatch(tail[0]))
    elif base == "make":
        targets = [arg for arg in argv[1:] if not arg.startswith("-")]
        allowed = bool(targets) and all(_SAFE_MAKE_TARGET.fullmatch(arg) for arg in targets)
    elif base == "go":
        allowed = len(argv) >= 2 and argv[1] == "test"
    elif base == "cargo":
        allowed = len(argv) >= 2 and argv[1] in {"test", "check", "clippy"}
    elif base == "dotnet":
        allowed = len(argv) >= 2 and argv[1] == "test"
    elif base in {"mvn", "mvnw"}:
        goals = [arg for arg in argv[1:] if not arg.startswith("-")]
        allowed = bool(goals) and all(arg in {"test", "verify"} for arg in goals)
    if not allowed:
        return None, (
            "only recognized test, lint, type-check, verification, and build entrypoints "
            "are allowed"
        )
    return argv, None


def _git(cwd: Path, *args: str, timeout: int = 30) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [
            "git",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.pager=cat",
            *args,
        ],
        cwd=str(cwd),
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _copy_working_tree_overlay(source_root: Path, snapshot_root: Path) -> None:
    listed = _git(source_root, "ls-files", "-co", "--exclude-standard", "-z")
    if listed.returncode != 0:
        raise RuntimeError((listed.stderr or listed.stdout or b"git ls-files failed").decode(errors="replace"))
    for raw in listed.stdout.split(b"\0"):
        if not raw:
            continue
        relative = Path(os.fsdecode(raw))
        source = source_root / relative
        target = snapshot_root / relative
        if not source.exists() and not source.is_symlink():
            if target.exists() or target.is_symlink():
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
        if source.is_symlink():
            target.symlink_to(os.readlink(source), target_is_directory=source.is_dir())
        else:
            shutil.copy2(source, target)

    deleted = _git(source_root, "ls-files", "--deleted", "-z")
    if deleted.returncode == 0:
        for raw in deleted.stdout.split(b"\0"):
            if not raw:
                continue
            target = snapshot_root / Path(os.fsdecode(raw))
            if target.exists() or target.is_symlink():
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
                else:
                    target.unlink()


def _bounded_output(value: bytes) -> str:
    text = value.decode("utf-8", errors="replace")
    if len(text) <= _OUTPUT_LIMIT:
        return text
    half = (_OUTPUT_LIMIT - 80) // 2
    return text[:half] + "\n...[verification output truncated]...\n" + text[-half:]


def _environment_name_is_sensitive(name: str) -> bool:
    upper = str(name or "").strip().upper()
    return bool(
        upper in _SENSITIVE_ENV_EXACT
        or upper.endswith(_SENSITIVE_ENV_SUFFIXES)
        or _SENSITIVE_ENV_NAME.search(upper)
    )


def _resolved_verification_argv(argv: list[str]) -> list[str]:
    """Resolve trusted executables without accepting user-supplied absolute paths."""

    resolved = list(argv)
    base = Path(resolved[0]).name.lower()
    if base in {"pytest", "py.test"}:
        return [sys.executable, "-m", "pytest", *resolved[1:]]
    if base in {"python", "python3"}:
        resolved[0] = sys.executable
        return resolved
    if "/" not in resolved[0]:
        executable = shutil.which(resolved[0])
        if executable:
            resolved[0] = executable
    return resolved


def read_only_verify(
    *,
    command: str,
    workdir: str = "",
    timeout: int = 300,
    runtime_mode: Any = None,
) -> str:
    """Run a recognized verification command in a disposable read-only sandbox."""

    if normalize_runtime_mode(runtime_mode) is not RuntimeMode.READ_ONLY:
        return tool_error("read_only_verify is available only in a read-only runtime")
    argv, error = parse_read_only_verification_command(command)
    if error or argv is None:
        return tool_error(f"Unsafe verification command: {error}")
    argv = _resolved_verification_argv(argv)

    raw_cwd = str(workdir or get_session_env("HERMES_SESSION_CWD", "") or os.getcwd())
    source_cwd = Path(raw_cwd).expanduser().resolve(strict=False)
    root_result = _git(source_cwd, "rev-parse", "--show-toplevel", timeout=10)
    if root_result.returncode != 0:
        return tool_error("read_only_verify requires a Git working tree")
    source_root = Path(root_result.stdout.decode(errors="replace").strip()).resolve()
    try:
        relative_cwd = source_cwd.relative_to(source_root)
    except ValueError:
        return tool_error("verification workdir is outside its Git root")

    try:
        timeout_value = max(1, min(int(timeout), 600))
    except (TypeError, ValueError):
        timeout_value = 300

    try:
        with tempfile.TemporaryDirectory(prefix="hermes-readonly-verify-") as temp_value:
            temp_root = Path(temp_value)
            snapshot_root = temp_root / "repo"
            clone = subprocess.run(
                [
                    "git",
                    "-c",
                    "core.fsmonitor=false",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "clone",
                    "--quiet",
                    "--no-hardlinks",
                    "--no-checkout",
                    "--config",
                    "core.fsmonitor=false",
                    "--config",
                    "core.hooksPath=/dev/null",
                    str(source_root),
                    str(snapshot_root),
                ],
                capture_output=True,
                timeout=60,
                check=False,
            )
            if clone.returncode != 0:
                return tool_error(
                    "Could not create disposable verification snapshot: "
                    + _bounded_output(clone.stderr or clone.stdout)
                )
            # Populate only the disposable index. A host-side checkout can run
            # repository-configured clean/smudge filters; the working files are
            # copied explicitly from the source tree below instead.
            read_tree = _git(snapshot_root, "read-tree", "HEAD", timeout=60)
            if read_tree.returncode != 0:
                return tool_error(
                    "Could not materialize disposable verification snapshot: "
                    + _bounded_output(read_tree.stderr or read_tree.stdout)
                )
            _copy_working_tree_overlay(source_root, snapshot_root)
            snapshot_cwd = snapshot_root / relative_cwd
            if not snapshot_cwd.is_dir():
                return tool_error("verification workdir is unavailable in the disposable snapshot")

            env = os.environ.copy()
            for key in list(env):
                if _environment_name_is_sensitive(key):
                    env.pop(key, None)
            env.update(
                {
                    "HOME": "/tmp/home",
                    "TMPDIR": "/tmp",
                    "XDG_CACHE_HOME": "/tmp/cache",
                    "XDG_CONFIG_HOME": "/tmp/config",
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "CI": "1",
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_GLOBAL": "/dev/null",
                    "GIT_TERMINAL_PROMPT": "0",
                }
            )
            sandbox_argv = [
                shutil.which("bwrap") or "bwrap",
                "--die-with-parent",
                "--unshare-net",
                "--unshare-pid",
                "--unshare-ipc",
                "--unshare-uts",
                "--ro-bind",
                "/",
                "/",
                # Hide host service/display sockets and give tests ordinary
                # disposable temp space. The repository itself is mounted at
                # a stable sandbox-only path below.
                "--tmpfs",
                "/run",
                "--tmpfs",
                "/tmp",
                "--tmpfs",
                "/var/tmp",
                "--dir",
                "/tmp/workspace",
                "--bind",
                str(snapshot_root),
                "/tmp/workspace",
                "--dir",
                "/tmp/home",
                "--dir",
                "/tmp/cache",
                "--dir",
                "/tmp/config",
                "--dev",
                "/dev",
                "--proc",
                "/proc",
                "--chdir",
                str(PurePosixPath("/tmp/workspace") / PurePosixPath(relative_cwd.as_posix())),
                "--",
                *argv,
            ]
            completed = subprocess.run(
                sandbox_argv,
                capture_output=True,
                timeout=timeout_value,
                check=False,
                env=env,
            )
            return json.dumps(
                {
                    "success": completed.returncode == 0,
                    "command": argv,
                    "exit_code": completed.returncode,
                    "output": _bounded_output(completed.stdout),
                    "error": _bounded_output(completed.stderr) or None,
                    "sandbox": (
                        "temporary snapshot; host filesystem read-only; network, PID, IPC, "
                        "and host runtime sockets isolated"
                    ),
                    "artifacts_cleaned": True,
                },
                ensure_ascii=False,
            )
    except subprocess.TimeoutExpired:
        return tool_error(f"Verification exceeded the {timeout_value}s timeout; temporary files were cleaned")
    except Exception as exc:
        return tool_error(f"Read-only verification failed closed: {type(exc).__name__}: {exc}")


READ_ONLY_VERIFY_SCHEMA = {
    "name": "read_only_verify",
    "description": (
        "Run a recognized test, lint, type-check, verification, or build command in a "
        "temporary Git snapshot. The host filesystem is mounted read-only, network access "
        "is disabled, credentials are removed, and all temporary artifacts are deleted."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "A single recognized verification command without shell operators.",
            },
            "workdir": {
                "type": "string",
                "description": "Optional absolute source working directory; defaults to the session cwd.",
            },
            "timeout": {
                "type": "integer",
                "minimum": 1,
                "maximum": 600,
                "default": 300,
            },
        },
        "required": ["command"],
    },
}


registry.register(
    name="read_only_verify",
    toolset="terminal",
    schema=READ_ONLY_VERIFY_SCHEMA,
    handler=lambda args, **kw: read_only_verify(
        command=args.get("command", ""),
        workdir=args.get("workdir", ""),
        timeout=args.get("timeout", 300),
        runtime_mode=kw.get("runtime_mode"),
    ),
    check_fn=check_read_only_verification_requirements,
    effect=ToolEffect.READ_ONLY,
    emoji="🧪",
    max_result_size_chars=_OUTPUT_LIMIT,
)
