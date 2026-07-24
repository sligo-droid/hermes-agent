"""Operator-configured, bounded project observations."""

from __future__ import annotations

import copy
import json
import os
import re
import resource
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.runtime_capabilities import ToolEffect
from tools.registry import registry, tool_error


_BASE_DESCRIPTION = (
    "List or run an operator-configured project observation. Each observation "
    "uses a fixed working directory and fixed subprocess argv from config; the "
    "model cannot supply commands, paths, environment variables, or shell text."
)
_SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
_OUTPUT_LIMIT = 64_000
_CAPTURE_LIMIT = 256_000
_ADDRESS_SPACE_LIMIT = 2 * 1024 * 1024 * 1024
_DEFAULT_TIMEOUT_SECONDS = 20
_MAX_TIMEOUT_SECONDS = 120
_MAX_OBSERVATIONS = 64
_MAX_OPTIONS = 8
_MAX_ARGV_ITEMS = 64
_MAX_OPTION_ARGV_ITEMS = 8
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_OPTION_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,39}$")
_OBSERVATION_KEYS = {
    "argv",
    "cwd",
    "description",
    "options",
    "timeout_seconds",
}
_OPTION_KEYS = {
    "default",
    "description",
    "false_argv",
    "true_argv",
    "type",
}


@dataclass(frozen=True)
class _BooleanOption:
    name: str
    description: str
    default: bool
    true_argv: tuple[str, ...]
    false_argv: tuple[str, ...]


@dataclass(frozen=True)
class _Observation:
    name: str
    description: str
    cwd: Path
    argv: tuple[str, ...]
    timeout_seconds: int
    options: dict[str, _BooleanOption]


def _load_observation_config() -> tuple[dict[str, Any], str | None]:
    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly()
    except Exception as exc:
        return {}, f"project observation config could not be loaded: {type(exc).__name__}"
    raw = config.get("project_observations", {}) if isinstance(config, dict) else {}
    if raw is None:
        return {}, None
    if not isinstance(raw, dict):
        return {}, "project_observations must be a mapping"
    if len(raw) > _MAX_OBSERVATIONS:
        return {}, f"project_observations is limited to {_MAX_OBSERVATIONS} entries"
    return raw, None


def _validate_argv(value: Any, *, limit: int) -> tuple[tuple[str, ...] | None, str | None]:
    if not isinstance(value, list) or not value:
        return None, "argv must be a non-empty list of strings"
    if len(value) > limit:
        return None, f"argv is limited to {limit} items"
    result: list[str] = []
    total_chars = 0
    for item in value:
        if not isinstance(item, str) or not item or "\x00" in item:
            return None, "argv entries must be non-empty strings without NUL bytes"
        total_chars += len(item)
        if len(item) > 4096 or total_chars > 32_768:
            return None, "argv exceeds the configured size limit"
        result.append(item)
    return tuple(result), None


def _parse_option(name: Any, value: Any) -> tuple[_BooleanOption | None, str | None]:
    if not isinstance(name, str) or not _OPTION_RE.fullmatch(name):
        return None, "option names must use letters, numbers, '_' or '-'"
    if not isinstance(value, dict):
        return None, f"option {name!r} must be a mapping"
    unknown = sorted(set(value) - _OPTION_KEYS)
    if unknown:
        return None, f"option {name!r} has unsupported keys: {', '.join(unknown)}"
    if value.get("type", "boolean") != "boolean":
        return None, f"option {name!r} must have type: boolean"
    default = value.get("default", False)
    if not isinstance(default, bool):
        return None, f"option {name!r} default must be boolean"
    true_argv, error = _validate_argv(
        value.get("true_argv", []),
        limit=_MAX_OPTION_ARGV_ITEMS,
    ) if value.get("true_argv") else ((), None)
    if error or true_argv is None:
        return None, f"option {name!r} true_argv: {error}"
    false_argv, error = _validate_argv(
        value.get("false_argv", []),
        limit=_MAX_OPTION_ARGV_ITEMS,
    ) if value.get("false_argv") else ((), None)
    if error or false_argv is None:
        return None, f"option {name!r} false_argv: {error}"
    description = str(value.get("description") or "Configured boolean option").strip()
    return (
        _BooleanOption(
            name=name,
            description=description[:300],
            default=default,
            true_argv=true_argv,
            false_argv=false_argv,
        ),
        None,
    )


def _resolve_cwd(value: Any) -> tuple[Path | None, str | None]:
    if not isinstance(value, str) or not value.strip():
        return None, "cwd must be a configured absolute directory"
    if "${" in value:
        return None, "cwd contains an unresolved environment template"
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        return None, "cwd must resolve to an absolute path"
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        return None, f"cwd is unavailable: {type(exc).__name__}"
    if not resolved.is_dir():
        return None, f"cwd is not a directory: {resolved}"
    return resolved, None


def _resolve_executable(argv0: str, cwd: Path) -> tuple[str | None, str | None]:
    if os.path.isabs(argv0):
        executable = Path(argv0).expanduser()
        invocation = str(executable)
    elif os.sep in argv0 or (os.altsep and os.altsep in argv0):
        lexical = Path(os.path.abspath(cwd / argv0))
        try:
            lexical.relative_to(cwd)
        except ValueError:
            return None, "relative executable must remain inside the configured cwd"
        executable = lexical
        invocation = argv0
    else:
        found = shutil.which(argv0, path=_SAFE_PATH)
        if not found:
            return None, f"configured executable is unavailable on the sanitized PATH: {argv0}"
        executable = Path(found)
        invocation = argv0
    if not executable.is_file() or not os.access(executable, os.X_OK):
        return None, f"configured executable is unavailable or not executable: {executable}"
    return invocation, None


def _parse_observation(name: Any, value: Any) -> tuple[_Observation | None, str | None]:
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        return None, "observation names must use letters, numbers, '.', '_' or '-'"
    if not isinstance(value, dict):
        return None, f"observation {name!r} must be a mapping"
    unknown = sorted(set(value) - _OBSERVATION_KEYS)
    if unknown:
        return None, f"observation {name!r} has unsupported keys: {', '.join(unknown)}"
    cwd, error = _resolve_cwd(value.get("cwd"))
    if error or cwd is None:
        return None, f"observation {name!r}: {error}"
    argv, error = _validate_argv(value.get("argv"), limit=_MAX_ARGV_ITEMS)
    if error or argv is None:
        return None, f"observation {name!r}: {error}"
    executable, error = _resolve_executable(argv[0], cwd)
    if error or executable is None:
        return None, f"observation {name!r}: {error}"
    argv = (executable, *argv[1:])
    try:
        timeout_seconds = int(value.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS))
    except (TypeError, ValueError):
        return None, f"observation {name!r}: timeout_seconds must be an integer"
    if not 1 <= timeout_seconds <= _MAX_TIMEOUT_SECONDS:
        return None, (
            f"observation {name!r}: timeout_seconds must be between 1 and "
            f"{_MAX_TIMEOUT_SECONDS}"
        )
    raw_options = value.get("options", {})
    if raw_options is None:
        raw_options = {}
    if not isinstance(raw_options, dict):
        return None, f"observation {name!r}: options must be a mapping"
    if len(raw_options) > _MAX_OPTIONS:
        return None, f"observation {name!r}: options is limited to {_MAX_OPTIONS} entries"
    options: dict[str, _BooleanOption] = {}
    for option_name, option_value in raw_options.items():
        option, error = _parse_option(option_name, option_value)
        if error or option is None:
            return None, f"observation {name!r}: {error}"
        options[option.name] = option
    description = str(value.get("description") or "Configured project observation").strip()
    return (
        _Observation(
            name=name,
            description=description[:500],
            cwd=cwd,
            argv=argv,
            timeout_seconds=timeout_seconds,
            options=options,
        ),
        None,
    )


def _configured_names(raw: dict[str, Any]) -> list[str]:
    return sorted(
        name
        for name in raw
        if isinstance(name, str) and _NAME_RE.fullmatch(name)
    )


def _build_dynamic_schema_overrides() -> dict[str, Any]:
    raw, config_error = _load_observation_config()
    names = _configured_names(raw)
    parameters = copy.deepcopy(PROJECT_OBSERVE_SCHEMA["parameters"])
    name_schema = parameters["properties"]["name"]
    if names:
        name_schema["enum"] = names
        summaries: list[str] = []
        option_properties: dict[str, Any] = {}
        for name in names:
            value = raw.get(name)
            description = ""
            if isinstance(value, dict):
                description = str(value.get("description") or "").strip()
                raw_options = value.get("options")
                if isinstance(raw_options, dict):
                    for option_name, option_value in raw_options.items():
                        if (
                            not isinstance(option_name, str)
                            or not _OPTION_RE.fullmatch(option_name)
                        ):
                            continue
                        option_description = "Configured boolean option"
                        default = False
                        if isinstance(option_value, dict):
                            option_description = str(
                                option_value.get("description") or option_description
                            ).strip()
                            if isinstance(option_value.get("default"), bool):
                                default = option_value["default"]
                        option_properties[option_name] = {
                            "type": "boolean",
                            "description": option_description[:300],
                            "default": default,
                        }
            summaries.append(f"{name}: {description[:160] or 'configured observation'}")
        parameters["properties"]["options"]["properties"] = option_properties
        available = " Available observations: " + "; ".join(summaries) + "."
    elif config_error:
        available = f" Observation registry is unavailable: {config_error}."
    else:
        available = " No project observations are configured; use operation='list' to confirm."
    return {
        "description": _BASE_DESCRIPTION + available,
        "parameters": parameters,
    }


def _minimal_environment() -> dict[str, str]:
    env = {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_PAGER": "cat",
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "NO_COLOR": "1",
        "PAGER": "cat",
        "PATH": _SAFE_PATH,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
        "TMPDIR": "/tmp",
    }
    for key in ("DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR"):
        value = os.environ.get(key)
        if value and "\x00" not in value and len(value) <= 4096:
            env[key] = value
    return env


def _resource_limits(timeout_seconds: int):
    def apply() -> None:
        os.umask(0o077)
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(
            resource.RLIMIT_AS,
            (_ADDRESS_SPACE_LIMIT, _ADDRESS_SPACE_LIMIT),
        )
        resource.setrlimit(resource.RLIMIT_FSIZE, (_CAPTURE_LIMIT, _CAPTURE_LIMIT))
        resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
        cpu = max(2, min(timeout_seconds + 5, _MAX_TIMEOUT_SECONDS + 5))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))

    return apply


def _kill_process_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _bounded_text(value: bytes) -> tuple[str, bool]:
    text = value.decode("utf-8", errors="replace")
    if len(text) <= _OUTPUT_LIMIT:
        return text, False
    half = (_OUTPUT_LIMIT - 64) // 2
    return (
        text[:half] + "\n...[project observation output truncated]...\n" + text[-half:],
        True,
    )


def _run_observation(spec: _Observation, options: Any) -> str:
    if options is None:
        options = {}
    if not isinstance(options, dict):
        return tool_error("options must be an object of configured boolean values")
    unknown_options = sorted(set(options) - set(spec.options))
    if unknown_options:
        return tool_error(
            f"Unknown option(s) for {spec.name}: {', '.join(unknown_options)}"
        )
    argv = list(spec.argv)
    selected_options: dict[str, bool] = {}
    for name, option in spec.options.items():
        value = options.get(name, option.default)
        if not isinstance(value, bool):
            return tool_error(f"Option {name!r} for {spec.name} must be boolean")
        selected_options[name] = value
        argv.extend(option.true_argv if value else option.false_argv)

    started = time.monotonic()
    timed_out = False
    return_code: int | None = None
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        try:
            process = subprocess.Popen(
                argv,
                cwd=str(spec.cwd),
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                env=_minimal_environment(),
                start_new_session=True,
                preexec_fn=_resource_limits(spec.timeout_seconds),
            )
        except OSError as exc:
            return tool_error(
                f"Observation {spec.name!r} could not start: {type(exc).__name__}"
            )
        try:
            return_code = process.wait(timeout=spec.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_process_group(process.pid)
            process.wait(timeout=5)
        finally:
            _kill_process_group(process.pid)
        stdout_file.seek(0)
        stderr_file.seek(0)
        stdout_bytes = stdout_file.read(_CAPTURE_LIMIT + 1)
        stderr_bytes = stderr_file.read(_CAPTURE_LIMIT + 1)

    stdout, stdout_truncated = _bounded_text(stdout_bytes)
    stderr, stderr_truncated = _bounded_text(stderr_bytes)
    parsed_output: Any = None
    if stdout and not stdout_truncated and len(stdout_bytes) <= _CAPTURE_LIMIT:
        try:
            parsed_output = json.loads(stdout)
        except (TypeError, ValueError):
            parsed_output = None
    result = {
        "success": return_code == 0 and not timed_out,
        "operation": "run",
        "observation": spec.name,
        "exit_code": return_code,
        "timed_out": timed_out,
        "duration_ms": round((time.monotonic() - started) * 1000),
        "stdout": stdout,
        "stderr": stderr or None,
        "output_truncated": (
            stdout_truncated
            or stderr_truncated
            or len(stdout_bytes) > _CAPTURE_LIMIT
            or len(stderr_bytes) > _CAPTURE_LIMIT
        ),
        "options": selected_options,
    }
    if parsed_output is not None:
        result["parsed_output"] = parsed_output
    if timed_out:
        result["error"] = f"Observation exceeded its {spec.timeout_seconds}s timeout"
    return json.dumps(result, ensure_ascii=False)


def project_observe(args: Any, *, runtime_mode: Any = None) -> str:
    """List or execute one fixed operator-configured observation."""

    del runtime_mode  # The fixed-command contract is observational in both modes.
    if not isinstance(args, dict):
        return tool_error("project_observe arguments must be an object")
    allowed = {"name", "operation", "options"}
    unknown = sorted(set(args) - allowed)
    if unknown:
        return tool_error(
            "Unsupported project_observe arguments: " + ", ".join(unknown)
        )
    operation = str(args.get("operation") or "").strip().lower()
    raw, config_error = _load_observation_config()
    if operation == "list":
        observations = []
        for name in _configured_names(raw):
            spec, error = _parse_observation(name, raw[name])
            observations.append(
                {
                    "name": name,
                    "description": (
                        spec.description
                        if spec is not None
                        else str(raw[name].get("description") or "")
                        if isinstance(raw[name], dict)
                        else ""
                    ),
                    "available": spec is not None,
                    "options": sorted(spec.options) if spec is not None else [],
                    "unavailable_reason": error,
                }
            )
        return json.dumps(
            {
                "success": config_error is None,
                "operation": "list",
                "observations": observations,
                "error": config_error,
            },
            ensure_ascii=False,
        )
    if operation != "run":
        return tool_error("operation must be one of: list, run")
    if config_error:
        return tool_error(config_error)
    name = args.get("name")
    if not isinstance(name, str) or not name:
        return tool_error("name is required for operation='run'")
    if name not in raw:
        return tool_error(f"Unknown project observation: {name}")
    spec, error = _parse_observation(name, raw[name])
    if error or spec is None:
        return tool_error(error or f"Project observation {name!r} is unavailable")
    return _run_observation(spec, args.get("options"))


PROJECT_OBSERVE_SCHEMA = {
    "name": "project_observe",
    "description": _BASE_DESCRIPTION,
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["list", "run"],
                "description": "List configured observations or run one by name.",
            },
            "name": {
                "type": "string",
                "description": "Configured observation name. Required for operation='run'.",
            },
            "options": {
                "type": "object",
                "additionalProperties": False,
                "properties": {},
                "description": (
                    "Optional configured boolean values only. Unknown options fail closed."
                ),
            },
        },
        "required": ["operation"],
    },
}


registry.register(
    name="project_observe",
    toolset="terminal",
    schema=PROJECT_OBSERVE_SCHEMA,
    handler=lambda args, **kw: project_observe(args, runtime_mode=kw.get("runtime_mode")),
    dynamic_schema_overrides=_build_dynamic_schema_overrides,
    effect=ToolEffect.READ_ONLY,
    emoji="📡",
    max_result_size_chars=_OUTPUT_LIMIT,
)
