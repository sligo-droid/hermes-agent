"""Disposable, network-isolated verification for read-only runtimes."""

from __future__ import annotations

import json
import os
import re
import resource
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from agent.runtime_capabilities import RuntimeMode, ToolEffect, normalize_runtime_mode
from gateway.session_context import get_session_env
from tools.registry import registry, tool_error


_SHELL_CONTROL_CHARS = frozenset("\n\r;|&<>`$*?[]{}")
_SAFE_SCRIPT_NAMES = re.compile(
    r"^(?:test|tests|lint|type[-_]?check|check|verify|verification|build)(?::[\w.-]+)?$",
    re.IGNORECASE,
)
_OUTPUT_LIMIT = 100_000
_CAPTURE_LIMIT = 1_000_000
_MEMORY_LIMIT_BYTES = 2 * 1024 * 1024 * 1024
_MEMORY_SWAP_LIMIT_BYTES = 512 * 1024 * 1024
_TASK_LIMIT = 256
_CGROUP_V2_ROOT = Path("/sys/fs/cgroup")
_cgroup_limiter_probe: bool | None = None
_VERIFICATION_ENV_ALLOWLIST = frozenset(
    {
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "TERM",
        "COLORTERM",
        "NO_COLOR",
        "FORCE_COLOR",
        "PYTHONHASHSEED",
        "HERMES_TEST_WORKERS",
    }
)


def check_read_only_verification_requirements() -> bool:
    return bool(
        shutil.which("git")
        and shutil.which("bwrap")
        and shutil.which("systemd-run")
        and (_CGROUP_V2_ROOT / "cgroup.controllers").is_file()
    )


def _cgroup_v2_limiter_available(*, probe: bool = True) -> bool:
    """Return whether aggregate memory/tasks limits can be enforced.

    RLIMIT_NPROC is scoped to the shared host UID and is therefore neither a
    precise nor safe per-verification process quota. A transient user scope
    gives the entire bwrap process tree one cgroup-v2 memory/tasks boundary.
    """

    global _cgroup_limiter_probe
    controllers_path = _CGROUP_V2_ROOT / "cgroup.controllers"
    systemd_run = shutil.which("systemd-run")
    try:
        controllers = set(controllers_path.read_text(encoding="utf-8").split())
    except OSError:
        return False
    if not systemd_run or not {"memory", "pids"}.issubset(controllers):
        return False
    if not probe:
        return True
    if _cgroup_limiter_probe is not None:
        return _cgroup_limiter_probe
    try:
        result = subprocess.run(
            [
                systemd_run,
                "--user",
                "--scope",
                "--quiet",
                "--collect",
                "-p",
                f"MemoryMax={_MEMORY_LIMIT_BYTES}",
                "-p",
                f"MemorySwapMax={_MEMORY_SWAP_LIMIT_BYTES}",
                "-p",
                f"TasksMax={_TASK_LIMIT}",
                "/usr/bin/true",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
        _cgroup_limiter_probe = result.returncode == 0
    except Exception:
        _cgroup_limiter_probe = False
    return _cgroup_limiter_probe


def _with_verification_cgroup(
    argv: list[str],
    *,
    command_env: dict[str, str] | None = None,
) -> list[str]:
    systemd_run = shutil.which("systemd-run")
    if not systemd_run or not _cgroup_v2_limiter_available():
        raise RuntimeError(
            "cgroup v2 memory and task limiters are unavailable; refusing verification"
        )
    wrapped = [
        systemd_run,
        "--user",
        "--scope",
        "--quiet",
        "--collect",
        "-p",
        f"MemoryMax={_MEMORY_LIMIT_BYTES}",
        "-p",
        f"MemorySwapMax={_MEMORY_SWAP_LIMIT_BYTES}",
        "-p",
        f"TasksMax={_TASK_LIMIT}",
    ]
    if command_env is not None:
        wrapped.extend(
            [
                "/usr/bin/env",
                "-i",
                *(f"{key}={value}" for key, value in sorted(command_env.items())),
            ]
        )
    return [*wrapped, *argv]


def _cgroup_launcher_environment() -> dict[str, str]:
    """Minimal host environment needed to reach the user systemd manager."""

    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    for key in ("HOME", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"):
        value = os.environ.get(key)
        if isinstance(value, str) and value:
            env[key] = value
    return env


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
    elif base in {"npm", "pnpm"}:
        tail = argv[1:]
        if tail and tail[0] == "run":
            tail = tail[1:]
        allowed = bool(tail and _SAFE_SCRIPT_NAMES.fullmatch(tail[0]))
    if not allowed:
        return None, (
            "only repository test wrappers, pytest, and npm/pnpm test, lint, "
            "type-check, verification, or build scripts are allowed"
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


def _neutralize_escaping_symlinks(snapshot_root: Path) -> list[str]:
    """Replace source symlinks that could leave the disposable repository."""

    neutralized: list[str] = []
    for path in snapshot_root.rglob("*"):
        if not path.is_symlink():
            continue
        try:
            target = Path(os.readlink(path))
            resolved = target if target.is_absolute() else path.parent / target
            resolved = resolved.resolve(strict=False)
            resolved.relative_to(snapshot_root)
        except (OSError, ValueError):
            relative = path.relative_to(snapshot_root).as_posix()
            path.unlink(missing_ok=True)
            path.write_text(
                "Hermes read-only verification neutralized a symlink outside the source root.\n",
                encoding="utf-8",
            )
            neutralized.append(relative)
    return neutralized


def _bounded_output(value: bytes) -> str:
    text = value.decode("utf-8", errors="replace")
    if len(text) <= _OUTPUT_LIMIT:
        return text
    half = (_OUTPUT_LIMIT - 80) // 2
    return text[:half] + "\n...[verification output truncated]...\n" + text[-half:]


def _resolved_verification_argv(
    argv: list[str],
    *,
    has_venv: bool,
    has_pnpm_runtime: bool,
) -> list[str]:
    """Resolve trusted executables without accepting user-supplied absolute paths."""

    resolved = list(argv)
    base = Path(resolved[0]).name.lower()
    if base in {"pytest", "py.test"}:
        executable = "/tmp/workspace/.venv/bin/python" if has_venv else "/usr/bin/python3"
        return [executable, "-m", "pytest", *resolved[1:]]
    if base in {"python", "python3"}:
        resolved[0] = "/tmp/workspace/.venv/bin/python" if has_venv else "/usr/bin/python3"
        return resolved
    if base == "pnpm":
        if not has_pnpm_runtime:
            raise RuntimeError("a prepared offline pnpm runtime is unavailable")
        return ["/usr/bin/node", "/opt/hermes-pnpm/bin/pnpm.cjs", *resolved[1:]]
    if "/" not in resolved[0]:
        executable = shutil.which(resolved[0])
        if executable:
            resolved[0] = f"/usr/bin/{Path(executable).name}"
    return resolved


def _prepared_pnpm_runtime() -> Path | None:
    """Find a complete Corepack-cached pnpm distribution for offline use."""

    cache_candidates: list[Path] = []
    configured = os.environ.get("COREPACK_HOME")
    if configured:
        cache_candidates.append(Path(configured).expanduser())
    xdg_cache = os.environ.get("XDG_CACHE_HOME")
    if xdg_cache:
        cache_candidates.append(Path(xdg_cache).expanduser() / "node" / "corepack")
    cache_candidates.append(Path.home() / ".cache" / "node" / "corepack")

    pnpm_shim = shutil.which("pnpm")
    if pnpm_shim:
        shim_path = Path(pnpm_shim).expanduser().absolute()
        if len(shim_path.parents) >= 3 and shim_path.parent.name == "bin":
            cache_candidates.append(
                shim_path.parents[2] / ".cache" / "node" / "corepack"
            )

    def version_key(path: Path) -> tuple[int, ...]:
        match = re.fullmatch(r"(\d+(?:\.\d+)*)", path.name)
        return tuple(int(part) for part in match.group(1).split(".")) if match else ()

    for cache_root in dict.fromkeys(path.resolve(strict=False) for path in cache_candidates):
        pnpm_root = cache_root / "pnpm"
        if not pnpm_root.is_dir():
            continue
        versions = sorted(
            (path for path in pnpm_root.iterdir() if version_key(path)),
            key=version_key,
            reverse=True,
        )
        for candidate in versions:
            if (
                (candidate / ".corepack").is_file()
                and (candidate / "bin" / "pnpm.cjs").is_file()
                and (candidate / "dist" / "pnpm.cjs").is_file()
            ):
                return candidate.resolve()
    return None


def _prepared_dependency_roots(source_root: Path) -> dict[str, Path]:
    """Locate trusted prepared dependencies for a linked worktree snapshot."""

    roots: dict[str, Path] = {}
    common = _git(
        source_root,
        "rev-parse",
        "--path-format=absolute",
        "--git-common-dir",
        timeout=10,
    )
    primary_root = source_root
    if common.returncode == 0:
        common_dir = Path(common.stdout.decode(errors="replace").strip()).resolve()
        if common_dir.name == ".git":
            primary_root = common_dir.parent

    repository_roots = tuple(dict.fromkeys((primary_root, source_root)))
    venv_candidates = [
        *(root / relative for root in repository_roots for relative in (".venv", "venv")),
        Path(sys.prefix),
    ]
    for candidate in venv_candidates:
        if (candidate / "bin" / "python").is_file():
            roots["venv"] = candidate.resolve()
            python_binary = (candidate / "bin" / "python").resolve()
            try:
                python_binary.relative_to(candidate.resolve())
            except ValueError:
                python_link = candidate / "bin" / "python"
                try:
                    raw_target = Path(os.readlink(python_link))
                except OSError:
                    raw_target = python_binary
                if raw_target.is_absolute() and len(raw_target.parents) >= 3:
                    roots["python_runtime"] = raw_target.parent.parent.parent
                else:
                    roots["python_runtime"] = python_binary.parent.parent
            break

    hermes_source = Path(__file__).resolve().parents[1]
    hermes_common = _git(
        hermes_source,
        "rev-parse",
        "--path-format=absolute",
        "--git-common-dir",
        timeout=10,
    )
    hermes_primary = hermes_source
    if hermes_common.returncode == 0:
        common_dir = Path(hermes_common.stdout.decode(errors="replace").strip()).resolve()
        if common_dir.name == ".git":
            hermes_primary = common_dir.parent

    for relative, key in ((Path("node_modules"), "node_modules"), (Path("ui-tui/node_modules"), "ui_node_modules")):
        for root in dict.fromkeys(
            (*repository_roots, hermes_primary, hermes_source)
        ):
            candidate = root / relative
            if candidate.is_dir():
                roots[key] = candidate.resolve()
                break
    pnpm_runtime = _prepared_pnpm_runtime()
    if pnpm_runtime is not None:
        roots["pnpm_runtime"] = pnpm_runtime
    return roots


def _verification_environment() -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key in _VERIFICATION_ENV_ALLOWLIST and isinstance(value, str)
    }
    env.update(
        {
            "PATH": (
                "/tmp/workspace/.venv/bin:/tmp/workspace/node_modules/.bin:"
                "/tmp/workspace/ui-tui/node_modules/.bin:/usr/local/bin:/usr/bin:/bin"
            ),
            "HOME": "/tmp/home",
            "TMPDIR": "/tmp",
            "XDG_CACHE_HOME": "/tmp/cache",
            "XDG_CONFIG_HOME": "/tmp/config",
            "PYTHONDONTWRITEBYTECODE": "1",
            "CI": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "LANG": env.get("LANG", "C.UTF-8"),
            "LC_ALL": env.get("LC_ALL", "C.UTF-8"),
            "TZ": env.get("TZ", "UTC"),
        }
    )
    return env


def _sandbox_system_mounts() -> list[str]:
    args = ["--ro-bind", "/usr", "/usr"]
    for path in (Path("/bin"), Path("/lib"), Path("/lib64"), Path("/sbin")):
        if path.is_symlink():
            args.extend(["--symlink", os.readlink(path), str(path)])
        elif path.exists():
            args.extend(["--ro-bind", str(path), str(path)])
    args.extend(["--dir", "/etc"])
    for path in (
        "/etc/ld.so.cache",
        "/etc/nsswitch.conf",
        "/etc/passwd",
        "/etc/group",
        "/etc/hosts",
        "/etc/resolv.conf",
        "/etc/ssl/certs",
    ):
        if Path(path).exists():
            args.extend(["--ro-bind", path, path])
    return args


def _sandbox_parent_dirs(path: Path) -> list[str]:
    parents = []
    current = path.parent
    while str(current) not in {"", "/"}:
        parents.append(str(current))
        current = current.parent
    args: list[str] = []
    for parent in reversed(parents):
        args.extend(["--dir", parent])
    return args


def _resource_limits(timeout_value: int):
    def apply() -> None:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_FSIZE, (_CAPTURE_LIMIT, _CAPTURE_LIMIT))
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
        cpu = max(2, min(timeout_value + 5, 605))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))

    return apply


def _run_bounded(
    argv: list[str],
    *,
    env: dict[str, str],
    timeout_value: int,
) -> tuple[int, bytes, bytes, bool]:
    argv = _with_verification_cgroup(argv, command_env=env)
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        process = subprocess.Popen(
            argv,
            stdout=stdout_file,
            stderr=stderr_file,
            env=_cgroup_launcher_environment(),
            start_new_session=True,
            preexec_fn=_resource_limits(timeout_value),
        )
        try:
            return_code = process.wait(timeout=timeout_value)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=10)
            raise
        stdout_file.seek(0)
        stderr_file.seek(0)
        stdout = stdout_file.read(_CAPTURE_LIMIT + 1)
        stderr = stderr_file.read(_CAPTURE_LIMIT + 1)
        truncated = len(stdout) > _OUTPUT_LIMIT or len(stderr) > _OUTPUT_LIMIT
        return return_code, stdout, stderr, truncated


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
    if not _cgroup_v2_limiter_available():
        return tool_error(
            "Read-only verification failed closed: cgroup v2 memory and task "
            "limiters are unavailable"
        )
    argv, error = parse_read_only_verification_command(command)
    if error or argv is None:
        return tool_error(f"Unsafe verification command: {error}")
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
            neutralized_symlinks = _neutralize_escaping_symlinks(snapshot_root)
            snapshot_cwd = snapshot_root / relative_cwd
            if not snapshot_cwd.is_dir():
                return tool_error("verification workdir is unavailable in the disposable snapshot")

            dependencies = _prepared_dependency_roots(source_root)
            if "venv" in dependencies:
                (snapshot_root / ".venv").mkdir(exist_ok=True)
            if "node_modules" in dependencies:
                (snapshot_root / "node_modules").mkdir(exist_ok=True)
            if "ui_node_modules" in dependencies:
                (snapshot_root / "ui-tui" / "node_modules").mkdir(parents=True, exist_ok=True)
            argv = _resolved_verification_argv(
                argv,
                has_venv="venv" in dependencies,
                has_pnpm_runtime="pnpm_runtime" in dependencies,
            )
            env = _verification_environment()
            sandbox_argv = [
                shutil.which("bwrap") or "bwrap",
                "--die-with-parent",
                "--unshare-net",
                "--unshare-pid",
                "--unshare-ipc",
                "--unshare-uts",
                *_sandbox_system_mounts(),
                # Hide host service/display sockets and give tests ordinary
                # disposable temp space. No host home/workspace tree is mounted.
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
            ]
            if "venv" in dependencies:
                sandbox_argv += [
                    "--ro-bind",
                    str(dependencies["venv"]),
                    "/tmp/workspace/.venv",
                ]
            if "python_runtime" in dependencies:
                runtime_root = dependencies["python_runtime"]
                sandbox_argv += [
                    *_sandbox_parent_dirs(runtime_root),
                    "--dir",
                    str(runtime_root),
                    "--ro-bind",
                    str(runtime_root),
                    str(runtime_root),
                ]
            if "node_modules" in dependencies:
                sandbox_argv += [
                    "--ro-bind",
                    str(dependencies["node_modules"]),
                    "/tmp/workspace/node_modules",
                ]
            if "ui_node_modules" in dependencies:
                sandbox_argv += [
                    "--ro-bind",
                    str(dependencies["ui_node_modules"]),
                    "/tmp/workspace/ui-tui/node_modules",
                ]
            if "pnpm_runtime" in dependencies:
                sandbox_argv += [
                    "--dir",
                    "/opt",
                    "--ro-bind",
                    str(dependencies["pnpm_runtime"]),
                    "/opt/hermes-pnpm",
                ]
            sandbox_argv += [
                "--chdir",
                str(PurePosixPath("/tmp/workspace") / PurePosixPath(relative_cwd.as_posix())),
                "--",
                *argv,
            ]
            return_code, stdout, stderr, output_truncated = _run_bounded(
                sandbox_argv,
                env=env,
                timeout_value=timeout_value,
            )
            return json.dumps(
                {
                    "success": return_code == 0,
                    "command": argv,
                    "exit_code": return_code,
                    "output": _bounded_output(stdout),
                    "error": _bounded_output(stderr) or None,
                    "output_truncated": output_truncated,
                    "neutralized_symlinks": neutralized_symlinks[:100],
                    "dependencies": sorted(dependencies),
                    "sandbox": (
                        "temporary snapshot; only system runtime and prepared dependencies "
                        "mounted read-only; host home/workspaces and network, PID, IPC, and "
                        "runtime sockets isolated; cgroup v2 aggregate limits: "
                        f"memory={_MEMORY_LIMIT_BYTES} bytes, swap={_MEMORY_SWAP_LIMIT_BYTES} "
                        f"bytes, tasks={_TASK_LIMIT}"
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
        "temporary Git snapshot. Only prepared dependencies and system runtimes are mounted "
        "read-only; network access is disabled, credentials are removed, and temporary "
        "artifacts are deleted."
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
