"""Isolated, redacted Hermes configuration for trusted same-UID workers.

This removes supported config/env bridges to client knowledge. It is not a
filesystem sandbox: a same-UID process can still enumerate readable host paths.
"""

from __future__ import annotations

import copy
import os
import shutil
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping
from urllib.parse import urlsplit

import yaml

from hermes_constants import reset_hermes_home_override, set_hermes_home_override


_BLOCKED_ENV_PREFIXES = ("HERMES_CLIENT_KNOWLEDGE_",)
_BLOCKED_ENV_KEYS = {
    "GBRAIN_HOME", "HERMES_HOME", "HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
    "XDG_DATA_HOME", "XDG_STATE_HOME", "TERMINAL_CWD",
}
_SAFE_ENV_KEYS = {
    "ALL_PROXY", "GH_CONFIG_DIR", "GIT_CONFIG_GLOBAL", "GIT_SSH_COMMAND", "HTTPS_PROXY",
    "HTTP_PROXY", "LANG", "LC_ALL", "NO_PROXY", "PATH",
    "SSH_AUTH_SOCK", "TZ",
}
_SAFE_ENV_PREFIXES = ("HERMES_CODEX_WORKER_", "HERMES_CODING_WORKER_")
_SECRET_CONFIG_TOKENS = (
    "api_key", "apikey", "client_secret", "credential", "env", "password",
    "private_key", "secret", "token",
)
_WORKER_ENV_OVERRIDE: ContextVar[dict[str, str] | None] = ContextVar(
    "_HERMES_WORKER_ENV_OVERRIDE", default=None
)
_WORKER_PROTECTED_PATHS: ContextVar[tuple[str, ...]] = ContextVar(
    "_HERMES_WORKER_PROTECTED_PATHS", default=()
)


def get_worker_environment_override() -> dict[str, str] | None:
    """Return the task-local worker subprocess environment, if active."""
    value = _WORKER_ENV_OVERRIDE.get()
    return dict(value) if value is not None else None


def _set_worker_environment_override(env: Mapping[str, str]) -> Token:
    return _WORKER_ENV_OVERRIDE.set({str(key): str(value) for key, value in env.items()})


def get_worker_protected_paths() -> tuple[str, ...]:
    """Return source/credential paths that worker bridges must never transmit."""
    return _WORKER_PROTECTED_PATHS.get()


def _absolute_client_paths(config: Mapping[str, Any] | None) -> tuple[str, ...]:
    source = config or {}
    client = source.get("client_knowledge") if isinstance(source, Mapping) else None
    values: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            for child in value.values():
                visit(child)
        elif isinstance(value, (list, tuple, set)):
            for child in value:
                visit(child)
        elif isinstance(value, str):
            raw = value.strip()
            if raw and Path(raw).expanduser().is_absolute():
                values.add(str(Path(raw).expanduser()))

    if isinstance(client, Mapping):
        visit(client)
    return tuple(sorted(values, key=lambda item: (-len(item), item)))


def _contains_protected_path(value: str, protected_paths: tuple[str, ...]) -> bool:
    return any(path and path in value for path in protected_paths)


def _redact_config_value(value: Any, protected_paths: tuple[str, ...]) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, child in value.items():
            key = str(raw_key)
            normalized = key.lower().replace("-", "_")
            if any(token in normalized for token in _SECRET_CONFIG_TOKENS):
                continue
            redacted = _redact_config_value(child, protected_paths)
            if redacted is not None:
                result[key] = redacted
        return result
    if isinstance(value, list):
        return [
            redacted
            for child in value
            if (redacted := _redact_config_value(child, protected_paths)) is not None
        ]
    if isinstance(value, tuple):
        return tuple(
            redacted
            for child in value
            if (redacted := _redact_config_value(child, protected_paths)) is not None
        )
    if isinstance(value, str) and _contains_protected_path(value, protected_paths):
        return None
    return copy.deepcopy(value)


def redacted_worker_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return the narrow worker config; never copy then selectively mask paths."""
    source = dict(config or {})
    protected_paths = _absolute_client_paths(source)
    result: dict[str, Any] = {
        "plugins": {"enabled": []},
        "delegation": {
            "denied_toolsets": ["client_knowledge"],
            "inherit_mcp_toolsets": False,
        },
        "tools": {"enabled": [], "disabled": ["client_knowledge"]},
    }
    for key in ("coding_worker", "model_tiers", "model", "approvals"):
        value = source.get(key)
        if isinstance(value, Mapping):
            result[key] = _redact_config_value(dict(value), protected_paths)
    source_terminal = source.get("terminal")
    result["terminal"] = {
        "backend": "local",
        "timeout": max(
            1,
            int(source_terminal.get("timeout", 180))
            if isinstance(source_terminal, Mapping)
            else 180,
        ),
        "home_mode": "profile",
        "env_passthrough": [],
        "shell_init_files": [],
        "auto_source_bashrc": False,
    }
    result.pop("projects", None)
    result.pop("client_knowledge", None)
    result.pop("honcho", None)
    return result


@dataclass(slots=True)
class WorkerRuntimeEnvelope:
    root: Path
    home: Path
    config: dict[str, Any]
    protected_paths: tuple[str, ...] = ()

    @classmethod
    def create(cls, config: Mapping[str, Any] | None = None) -> "WorkerRuntimeEnvelope":
        root = Path(tempfile.mkdtemp(prefix="hermes-worker-runtime-"))
        root.chmod(0o700)
        home = root / "home"
        home.mkdir(mode=0o700)
        for name in ("config", "cache", "data", "state"):
            (root / name).mkdir(mode=0o700)
        protected_paths = _absolute_client_paths(config)
        payload = redacted_worker_config(config)
        config_path = home / "config.yaml"
        config_path.write_text(
            yaml.safe_dump(payload, sort_keys=True), encoding="utf-8"
        )
        config_path.chmod(0o600)
        runtime = cls(
            root=root,
            home=home,
            config=payload,
            protected_paths=protected_paths,
        )
        runtime.assert_paths_absent(list(protected_paths))
        return runtime

    def environment(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        env: dict[str, str] = {}
        for key, value in (base or {}).items():
            text = str(value)
            if (
                key in _BLOCKED_ENV_KEYS
                or any(key.startswith(prefix) for prefix in _BLOCKED_ENV_PREFIXES)
                or not (
                    key in _SAFE_ENV_KEYS
                    or any(key.startswith(prefix) for prefix in _SAFE_ENV_PREFIXES)
                )
                or _contains_protected_path(text, self.protected_paths)
            ):
                continue
            if key.upper() in {"ALL_PROXY", "HTTP_PROXY", "HTTPS_PROXY"}:
                try:
                    parsed = urlsplit(text)
                except ValueError:
                    continue
                if parsed.username is not None or parsed.password is not None:
                    continue
            env[key] = text
        env.update(
            {
                "HOME": str(self.home),
                "HERMES_HOME": str(self.home),
                "XDG_CONFIG_HOME": str(self.root / "config"),
                "XDG_CACHE_HOME": str(self.root / "cache"),
                "XDG_DATA_HOME": str(self.root / "data"),
                "XDG_STATE_HOME": str(self.root / "state"),
            }
        )
        return env

    @contextmanager
    def bind(self) -> Iterator[None]:
        home_token = set_hermes_home_override(self.home)
        env_token = _set_worker_environment_override(self.environment(os.environ))
        paths_token = _WORKER_PROTECTED_PATHS.set(self.protected_paths)
        try:
            yield
        finally:
            _WORKER_PROTECTED_PATHS.reset(paths_token)
            _WORKER_ENV_OVERRIDE.reset(env_token)
            reset_hermes_home_override(home_token)

    def assert_paths_absent(self, sensitive_paths: list[str]) -> None:
        serialized = yaml.safe_dump(self.config, sort_keys=True)
        env_blob = "\n".join(f"{key}={value}" for key, value in self.environment().items())
        for raw in sensitive_paths:
            value = str(raw or "").strip()
            if value and (value in serialized or value in env_blob):
                raise ValueError("worker runtime contains a protected client-knowledge path")

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


__all__ = [
    "WorkerRuntimeEnvelope", "get_worker_environment_override",
    "get_worker_protected_paths", "redacted_worker_config",
]
