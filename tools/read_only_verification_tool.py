"""Disposable, network-isolated verification for read-only runtimes."""

from __future__ import annotations

import json
import os
import posixpath
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

from agent.runtime_capabilities import ToolEffect
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
_PACKAGE_JSON_LIMIT = 1_000_000
_PNPM_PACKAGE_MANAGER_RE = re.compile(
    r"^pnpm@(?P<version>\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)(?:\+\S+)?$"
)
_GITHUB_HTTPS_ORIGIN_RE = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/"
    r"(?P<name>[A-Za-z0-9_.-]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)
_GITHUB_SSH_ORIGIN_RE = re.compile(
    r"^(?:ssh://)?git@github\.com(?::|/)(?P<owner>[A-Za-z0-9_.-]+)/"
    r"(?P<name>[A-Za-z0-9_.-]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)
_JAVASCRIPT_LOCK_NAMES = ("pnpm-lock.yaml", "package-lock.json", "yarn.lock")
_WRITABLE_NODE_CACHE_DIRS = (".vite", ".vite-temp")
_SANDBOX_RESERVED_MOUNT_PREFIXES = tuple(
    Path(value)
    for value in (
        "/usr",
        "/bin",
        "/lib",
        "/lib64",
        "/sbin",
        "/etc",
        "/proc",
        "/dev",
        "/run",
        "/opt",
        "/var",
    )
)
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
        _trusted_executable("git")
        and shutil.which("bwrap")
        and shutil.which("systemd-run")
        and (_CGROUP_V2_ROOT / "cgroup.controllers").is_file()
    )


def check_main_parent_verification_requirements() -> bool:
    return bool(_trusted_executable("git") and _trusted_executable("gh"))


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
    elif argv == ["git", "diff", "--check"]:
        allowed = True
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


def _trusted_executable(name: str) -> str | None:
    return shutil.which(name, path=os.defpath)


def _git_environment() -> dict[str, str]:
    return {
        "PATH": os.defpath,
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }


def _git(cwd: Path, *args: str, timeout: int = 30) -> subprocess.CompletedProcess[bytes]:
    executable = _trusted_executable("git")
    if not executable:
        raise RuntimeError("trusted Git executable is unavailable")
    return subprocess.run(
        [
            executable,
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
        env=_git_environment(),
        timeout=timeout,
        check=False,
    )


def _github_origin_repository(cwd: Path) -> str | None:
    remote = _git(cwd, "config", "--local", "--get", "remote.origin.url", timeout=10)
    if remote.returncode != 0:
        return None
    origin = remote.stdout.decode(errors="replace").strip()
    for pattern in (_GITHUB_HTTPS_ORIGIN_RE, _GITHUB_SSH_ORIGIN_RE):
        match = pattern.fullmatch(origin)
        if match:
            owner = match.group("owner")
            name = match.group("name").removesuffix(".git")
            if owner not in {".", ".."} and name not in {"", ".", ".."}:
                return f"{owner}/{name}".lower()
    return None


def _github_environment() -> dict[str, str]:
    env = {
        "PATH": os.defpath,
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "GH_PROMPT_DISABLED": "1",
    }
    for key in (
        "HOME",
        "XDG_CONFIG_HOME",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
    ):
        value = os.environ.get(key)
        if value:
            env[key] = value
    return env


def _github_main_sha(repository: str, cwd: Path) -> tuple[str | None, bytes]:
    gh = _trusted_executable("gh")
    if not gh:
        return None, b"trusted gh executable is unavailable"
    result = subprocess.run(
        [
            gh,
            "api",
            "--hostname",
            "github.com",
            f"repos/{repository}/git/ref/heads/main",
            "--jq",
            ".object.sha",
        ],
        cwd=str(cwd),
        capture_output=True,
        env=_github_environment(),
        timeout=30,
        check=False,
    )
    sha = result.stdout.decode(errors="replace").strip().lower()
    if result.returncode != 0 or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", sha):
        return None, result.stderr or result.stdout or b"GitHub returned an invalid main SHA"
    return sha, b""


def _github_pull_state(
    repository: str,
    pr_number: int,
    cwd: Path,
) -> tuple[dict[str, Any] | None, bytes]:
    gh = _trusted_executable("gh")
    if not gh:
        return None, b"trusted gh executable is unavailable"
    result = subprocess.run(
        [
            gh,
            "api",
            "--hostname",
            "github.com",
            f"repos/{repository}/pulls/{pr_number}",
        ],
        cwd=str(cwd),
        capture_output=True,
        env=_github_environment(),
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        return None, result.stderr or result.stdout or b"GitHub pull request lookup failed"
    try:
        payload = json.loads(result.stdout.decode(errors="replace"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None, b"GitHub returned invalid pull request JSON"
    if not isinstance(payload, dict) or int(payload.get("number") or 0) != pr_number:
        return None, b"GitHub returned an unexpected pull request"
    return payload, b""


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
        tail = resolved[1:]
        script_index = 1 if tail[:1] == ["run"] else 0
        separator_index = script_index + 1
        if (
            len(tail) > separator_index + 1
            and tail[separator_index] == "--"
            and all(not value.startswith("-") for value in tail[separator_index + 1 :])
        ):
            tail = tail[:separator_index] + tail[separator_index + 1 :]
        return ["/usr/bin/node", "/opt/hermes-pnpm/bin/pnpm.cjs", *tail]
    if "/" not in resolved[0]:
        executable = shutil.which(resolved[0])
        if executable:
            resolved[0] = f"/usr/bin/{Path(executable).name}"
    return resolved


def _pnpm_cache_roots() -> tuple[Path, ...]:
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

    return tuple(
        dict.fromkeys(path.resolve(strict=False) for path in cache_candidates)
    )


def _prepared_pnpm_runtime(version: str) -> Path | None:
    """Find the exact complete Corepack-cached pnpm distribution requested."""

    if not _PNPM_PACKAGE_MANAGER_RE.fullmatch(f"pnpm@{version}"):
        return None
    for cache_root in _pnpm_cache_roots():
        pnpm_root = cache_root / "pnpm"
        candidate = pnpm_root / version
        if (
            (candidate / ".corepack").is_file()
            and (candidate / "bin" / "pnpm.cjs").is_file()
            and (candidate / "dist" / "pnpm.cjs").is_file()
        ):
            return candidate.resolve()
    return None


def _pnpm_package_manager_pin(
    source_cwd: Path,
    source_root: Path,
) -> tuple[str | None, Path | None, str | None]:
    """Resolve the nearest repository packageManager field for a pnpm command."""

    current = source_cwd
    while True:
        manifest = current / "package.json"
        if manifest.is_file():
            try:
                if manifest.stat().st_size > _PACKAGE_JSON_LIMIT:
                    return None, manifest, "package.json is too large to inspect safely"
                data = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                return None, manifest, f"could not parse package.json: {type(exc).__name__}"
            if isinstance(data, dict) and "packageManager" in data:
                package_manager = str(data.get("packageManager") or "").strip()
                match = _PNPM_PACKAGE_MANAGER_RE.fullmatch(package_manager)
                if match is None:
                    return None, manifest, (
                        "packageManager must pin an exact pnpm runtime such as "
                        "pnpm@10.25.0"
                    )
                return match.group("version"), manifest, None
        if current == source_root:
            break
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None, None, (
        "pnpm verification requires an exact packageManager pin in the relevant "
        "repository package.json"
    )


def _prepared_dependency_roots(
    source_root: Path,
    *,
    source_cwd: Path | None = None,
    pnpm_runtime: Path | None = None,
) -> dict[str, Path]:
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

    node_modules = _prepared_node_modules(
        source_root,
        source_cwd or source_root,
        primary_root,
    )
    if node_modules is not None:
        modules_path, modules_relative = node_modules
        roots["node_modules"] = modules_path
        roots["node_modules_relative"] = modules_relative
    if pnpm_runtime is not None:
        roots["pnpm_runtime"] = pnpm_runtime.resolve()
    return roots


def _nearest_ancestor_with_file(
    start: Path,
    stop: Path,
    names: tuple[str, ...],
) -> tuple[Path | None, str | None]:
    current = start
    while True:
        for name in names:
            if (current / name).is_file():
                return current, name
        if current == stop or current.parent == current:
            return None, None
        current = current.parent


def _same_file(left: Path, right: Path) -> bool:
    try:
        return left.read_bytes() == right.read_bytes()
    except OSError:
        return False


def _prepared_node_modules(
    source_root: Path,
    source_cwd: Path,
    primary_root: Path,
) -> tuple[Path, Path] | None:
    """Find dependencies for the actual verification package/lock context."""

    package_root, _ = _nearest_ancestor_with_file(
        source_cwd,
        source_root,
        ("package.json",),
    )
    if package_root is None:
        return None
    lock_root, lock_name = _nearest_ancestor_with_file(
        package_root,
        source_root,
        _JAVASCRIPT_LOCK_NAMES,
    )

    context_roots = [package_root]
    if lock_root is not None and lock_root != package_root:
        context_roots.append(lock_root)

    candidates: list[tuple[Path, Path]] = []
    for context_root in context_roots:
        relative = context_root.relative_to(source_root)
        candidates.append((context_root, relative))
        if primary_root != source_root:
            primary_context = primary_root / relative
            if lock_root is not None and lock_name is not None:
                primary_lock_root = primary_root / lock_root.relative_to(source_root)
                if not _same_file(
                    lock_root / lock_name,
                    primary_lock_root / lock_name,
                ):
                    continue
            candidates.append((primary_context, relative))

    for context_root, relative in candidates:
        modules = context_root / "node_modules"
        if modules.is_dir():
            return modules.resolve(), relative / "node_modules"
    return None


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


def _iter_pnpm_package_links(node_modules: Path):
    """Yield bounded top-level/scoped package symlinks without entering .pnpm."""

    yielded = 0
    try:
        entries = tuple(node_modules.iterdir())
    except OSError:
        return
    for entry in entries[:4096]:
        if entry.is_symlink():
            yield entry
            yielded += 1
        elif entry.name.startswith("@") and entry.is_dir():
            try:
                scoped = tuple(entry.iterdir())
            except OSError:
                continue
            for package in scoped[:4096 - yielded]:
                if package.is_symlink():
                    yield package
                    yielded += 1
        if yielded >= 4096:
            return


def _pnpm_store_aliases(
    node_modules: Path,
    sandbox_node_modules: Path,
) -> tuple[Path, ...]:
    """Find sandbox aliases needed by relocated pnpm package symlinks."""

    store = (node_modules / ".pnpm").resolve(strict=False)
    if not store.is_dir():
        return ()
    aliases: list[Path] = []
    for link in _iter_pnpm_package_links(node_modules):
        try:
            suffix = link.resolve(strict=False).relative_to(store)
            raw_target = Path(os.readlink(link))
        except (OSError, ValueError):
            continue
        sandbox_link = sandbox_node_modules / link.relative_to(node_modules)
        if raw_target.is_absolute():
            sandbox_target = raw_target
        else:
            sandbox_target = Path(
                posixpath.normpath(str(sandbox_link.parent / raw_target))
            )
        alias = sandbox_target
        for _part in suffix.parts:
            alias = alias.parent
        if not alias.is_absolute() or alias == Path("/"):
            continue
        try:
            alias.relative_to(sandbox_node_modules)
        except ValueError:
            pass
        else:
            continue
        if any(
            alias == path or path in alias.parents
            for path in _SANDBOX_RESERVED_MOUNT_PREFIXES
        ):
            continue
        if alias not in aliases:
            aliases.append(alias)
        if len(aliases) >= 16:
            break
    return tuple(aliases)


def _node_modules_sandbox_args(
    node_modules: Path,
    relative: Path,
    temp_root: Path,
) -> list[str]:
    """Mount dependencies read-only with only standard Vite caches writable."""

    destination = Path("/tmp/workspace") / relative
    overlay_root = temp_root / "node-modules-overlay"
    skeleton = overlay_root / "skeleton"
    caches = overlay_root / "caches"
    skeleton.mkdir(parents=True, exist_ok=True)
    caches.mkdir(parents=True, exist_ok=True)
    for name in _WRITABLE_NODE_CACHE_DIRS:
        (skeleton / name).mkdir()
        (caches / name).mkdir()

    args = [
        "--dir",
        str(destination),
        "--overlay-src",
        str(node_modules),
        "--overlay-src",
        str(skeleton),
        "--ro-overlay",
        str(destination),
    ]
    store = (node_modules / ".pnpm").resolve(strict=False)
    if store.is_dir():
        for alias in _pnpm_store_aliases(node_modules, destination):
            args += [
                *_sandbox_parent_dirs(alias),
                "--dir",
                str(alias),
                "--ro-bind",
                str(store),
                str(alias),
            ]
    for name in _WRITABLE_NODE_CACHE_DIRS:
        args += [
            "--bind",
            str(caches / name),
            str(destination / name),
        ]
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

    del runtime_mode  # The sandbox contract is observational in either mode.
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

    pnpm_runtime: Path | None = None
    if Path(argv[0]).name.lower() == "pnpm":
        pnpm_version, manifest, pin_error = _pnpm_package_manager_pin(
            source_cwd,
            source_root,
        )
        if pin_error or pnpm_version is None:
            location = f" ({manifest})" if manifest is not None else ""
            return tool_error(f"Unsafe pnpm verification{location}: {pin_error}")
        pnpm_runtime = _prepared_pnpm_runtime(pnpm_version)
        if pnpm_runtime is None:
            return tool_error(
                f"Exact Corepack pnpm runtime {pnpm_version} required by {manifest} "
                "is not cached; refusing offline verification"
            )

    if not _cgroup_v2_limiter_available():
        return tool_error(
            "Read-only verification failed closed: cgroup v2 memory and task "
            "limiters are unavailable"
        )

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

            dependencies = _prepared_dependency_roots(
                source_root,
                source_cwd=source_cwd,
                pnpm_runtime=pnpm_runtime,
            )
            if "venv" in dependencies:
                (snapshot_root / ".venv").mkdir(exist_ok=True)
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
                sandbox_argv += _node_modules_sandbox_args(
                    dependencies["node_modules"],
                    dependencies["node_modules_relative"],
                    temp_root,
                )
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
            dependency_labels = [
                key
                for key in dependencies
                if key not in {"node_modules", "node_modules_relative"}
            ]
            if "node_modules" in dependencies:
                dependency_labels.append(
                    "node_modules:"
                    + dependencies["node_modules_relative"].as_posix()
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
                    "dependencies": sorted(dependency_labels),
                    "sandbox": (
                        "temporary snapshot; only system runtime and prepared dependencies "
                        "mounted read-only, with disposable writable node_modules/.vite and "
                        ".vite-temp caches when JavaScript dependencies are present; host "
                        "home and non-dependency workspace paths plus network, PID, IPC, and "
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


def verify_main_parent(
    *,
    pr_number: int,
    workdir: str = "",
    runtime_mode: Any = None,
) -> str:
    """Verify a closed PR head and parent against its GitHub repository."""

    del runtime_mode
    raw_cwd = str(workdir or get_session_env("HERMES_SESSION_CWD", "") or os.getcwd())
    source_cwd = Path(raw_cwd).expanduser().resolve(strict=False)
    try:
        root_result = _git(source_cwd, "rev-parse", "--show-toplevel", timeout=10)
    except Exception as exc:
        return tool_error(f"verify_main_parent failed closed: {type(exc).__name__}: {exc}")
    if root_result.returncode != 0:
        return tool_error("verify_main_parent requires a Git working tree")
    source_root = Path(root_result.stdout.decode(errors="replace").strip()).resolve()
    repository = _github_origin_repository(source_root)
    if not repository:
        return tool_error("origin must be a canonical github.com repository")
    try:
        normalized_pr_number = int(pr_number)
    except (TypeError, ValueError):
        return tool_error("pr_number must be a positive integer")
    if normalized_pr_number <= 0:
        return tool_error("pr_number must be a positive integer")
    pull, pull_error = _github_pull_state(
        repository,
        normalized_pr_number,
        source_root,
    )
    remote_sha, remote_error = _github_main_sha(repository, source_root)
    local = _git(
        source_root,
        "--no-replace-objects",
        "rev-parse",
        "HEAD",
        "HEAD^",
        timeout=10,
    )
    if pull is None or remote_sha is None or local.returncode != 0:
        detail = pull_error or remote_error or local.stderr or local.stdout
        return tool_error(
            "Could not verify PR head against origin/main: " + _bounded_output(detail)
        )
    local_shas = local.stdout.decode(errors="replace").splitlines()
    if len(local_shas) != 2 or not all(
        re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", sha.strip().lower())
        for sha in local_shas
    ):
        return tool_error("Git returned ambiguous local commit identities")
    head_sha, parent_sha = [sha.strip().lower() for sha in local_shas]
    pull_head = pull.get("head") if isinstance(pull.get("head"), dict) else {}
    pull_base = pull.get("base") if isinstance(pull.get("base"), dict) else {}
    pr_head_sha = str(pull_head.get("sha") or "").strip().lower()
    base_ref = str(pull_base.get("ref") or "").strip()
    state = str(pull.get("state") or "").strip().lower()
    merged = pull.get("merged") is True or pull.get("merged_at") is not None
    pr_verified = bool(
        state == "closed"
        and not merged
        and base_ref == "main"
        and re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", pr_head_sha)
    )
    branch_matches = bool(
        head_sha == pr_head_sha
        and remote_sha == parent_sha
    )
    matches = pr_verified and branch_matches
    return json.dumps(
        {
            "success": matches,
            "exit_code": 0 if matches else 1,
            "error": None,
            "repository": repository,
            "repository_root": str(source_root),
            "pr_number": normalized_pr_number,
            "head_sha": head_sha,
            "pr_evidence": {
                "status": "success" if pr_verified else "failure",
                "state": state,
                "merged": merged,
                "base_ref": base_ref,
                "head_sha": pr_head_sha,
            },
            "main_branch_evidence": {
                "status": "success" if branch_matches else "failure",
                "remote_main": remote_sha,
                "commit_parent": parent_sha,
            },
        },
        ensure_ascii=False,
    )


VERIFY_MAIN_PARENT_SCHEMA = {
    "name": "verify_main_parent",
    "description": (
        "Directly verify a specific closed, unmerged GitHub PR head against local HEAD and "
        "compare origin/main with local HEAD^, returning typed PR-bound evidence."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "pr_number": {
                "type": "integer",
                "minimum": 1,
                "description": "Pull request number to verify in the repository's origin.",
            },
            "workdir": {
                "type": "string",
                "description": "Optional absolute source working directory; defaults to session cwd.",
            },
        },
        "required": ["pr_number"],
    },
}


registry.register(
    name="verify_main_parent",
    toolset="terminal",
    schema=VERIFY_MAIN_PARENT_SCHEMA,
    handler=lambda args, **kw: verify_main_parent(
        pr_number=args.get("pr_number", 0),
        workdir=args.get("workdir", ""),
        runtime_mode=kw.get("runtime_mode"),
    ),
    check_fn=check_main_parent_verification_requirements,
    effect=ToolEffect.READ_ONLY,
    emoji="🔎",
    max_result_size_chars=10_000,
)
