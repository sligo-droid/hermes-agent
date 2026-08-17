"""Operator-owned browser authentication profiles with secret-safe loading."""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from hermes_constants import get_hermes_home


_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_LOOPBACK_PORT_PATTERN_RE = re.compile(r"^http://(?:127\.0\.0\.1|\[::1\]):\*$")


class BrowserAuthProfileError(ValueError):
    """Safe configuration/load failure without credential-bearing detail."""


@dataclass(frozen=True)
class BrowserAuthProfile:
    name: str
    origins: tuple[str, ...]
    origin_patterns: tuple[str, ...]
    env_file: Path
    username_env: str
    password_env: str
    username_selector: str
    password_selector: str
    submit_selector: str
    success_selector: str
    timeout_s: float


def _browser_config(config: Any = None) -> dict[str, Any]:
    if isinstance(config, dict):
        raw = config
    else:
        from hermes_cli.config import read_raw_config

        raw = read_raw_config()
    browser = raw.get("browser") if isinstance(raw, dict) else None
    return browser if isinstance(browser, dict) else {}


def configured_browser_auth_profile_names(config: Any = None) -> tuple[str, ...]:
    profiles = _browser_config(config).get("auth_profiles")
    if not isinstance(profiles, dict):
        return ()
    return tuple(
        sorted(
            name
            for raw_name in profiles
            if (name := str(raw_name or "").strip())
            and _PROFILE_NAME_RE.fullmatch(name)
        )
    )


def matching_browser_auth_profile_names(
    origin: str,
    *,
    config: Any = None,
) -> tuple[str, ...]:
    """Return valid opaque profile names configured for an exact origin."""

    normalized_origin = _normalize_origin(origin)
    profiles = _browser_config(config).get("auth_profiles")
    if not isinstance(profiles, dict):
        return ()
    matches: list[str] = []
    for raw_name, raw in profiles.items():
        name = str(raw_name or "").strip()
        try:
            profile = _profile_from_config(name, raw)
        except BrowserAuthProfileError:
            continue
        if _profile_matches_origin(profile, normalized_origin):
            matches.append(profile.name)
    return tuple(sorted(matches))


def _normalize_origin(value: Any) -> str:
    text = str(value or "").strip()
    try:
        parsed = urlsplit(text)
        parsed_port = parsed.port
    except ValueError:
        raise BrowserAuthProfileError(
            "browser auth profile has an invalid origin"
        ) from None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or parsed_port == 0
    ):
        raise BrowserAuthProfileError("browser auth profile has an invalid origin")
    host = parsed.hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = f":{parsed_port}" if parsed_port is not None else ""
    return f"{parsed.scheme.lower()}://{host}{port}"


def _normalize_origin_pattern(value: Any) -> str:
    text = str(value or "").strip().lower()
    if _LOOPBACK_PORT_PATTERN_RE.fullmatch(text):
        return text
    try:
        parsed = urlsplit(text)
        parsed_port = parsed.port
    except ValueError:
        raise BrowserAuthProfileError(
            "browser auth profile has an invalid origin pattern"
        ) from None
    host = parsed.hostname or ""
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed_port is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or host.count("*") != 1
    ):
        raise BrowserAuthProfileError(
            "browser auth profile has an invalid origin pattern"
        )
    wildcard_label = next((label for label in host.split(".") if "*" in label), "")
    prefix, _, suffix = wildcard_label.partition("*")
    if (
        len(host.split(".")) < 3
        or len(prefix.replace("-", "")) < 3
        or len(suffix.replace("-", "")) < 3
        or not re.fullmatch(r"[a-z0-9.*-]+", host)
    ):
        raise BrowserAuthProfileError(
            "browser auth origin pattern must be a narrow HTTPS hostname pattern"
        )
    return f"https://{host}"


def _origin_matches_pattern(origin: str, pattern: str) -> bool:
    if _LOOPBACK_PORT_PATTERN_RE.fullmatch(pattern):
        prefix = pattern[:-1]
        return origin.startswith(prefix) and origin[len(prefix) :].isdigit()
    expression = re.escape(pattern).replace(r"\*", r"[a-z0-9-]+")
    return re.fullmatch(expression, origin) is not None


def _profile_matches_origin(profile: BrowserAuthProfile, origin: str) -> bool:
    return origin in profile.origins or any(
        _origin_matches_pattern(origin, pattern)
        for pattern in profile.origin_patterns
    )


def _selector(raw: Any, label: str) -> str:
    value = str(raw or "").strip()
    if not value or len(value) > 200 or any(ord(char) < 32 for char in value):
        raise BrowserAuthProfileError(f"browser auth profile has an invalid {label}")
    return value


def _profile_from_config(name: str, raw: Any) -> BrowserAuthProfile:
    if not _PROFILE_NAME_RE.fullmatch(name) or not isinstance(raw, dict):
        raise BrowserAuthProfileError("browser auth profile is invalid")
    origins_raw = raw.get("origins", [])
    if isinstance(origins_raw, str):
        origins_raw = [origins_raw]
    if not isinstance(origins_raw, (list, tuple)):
        raise BrowserAuthProfileError("browser auth profile has invalid origins")
    origins = tuple(dict.fromkeys(_normalize_origin(item) for item in origins_raw))
    patterns_raw = raw.get("origin_patterns", [])
    if isinstance(patterns_raw, str):
        patterns_raw = [patterns_raw]
    if not isinstance(patterns_raw, (list, tuple)):
        raise BrowserAuthProfileError(
            "browser auth profile has invalid origin patterns"
        )
    origin_patterns = tuple(
        dict.fromkeys(_normalize_origin_pattern(item) for item in patterns_raw)
    )
    if not origins and not origin_patterns:
        raise BrowserAuthProfileError("browser auth profile has no origins")

    secrets_root = (get_hermes_home() / "secrets").resolve()
    env_value = str(raw.get("env_file") or "").strip()
    if not env_value:
        raise BrowserAuthProfileError("browser auth profile has no credential file")
    env_file = Path(env_value).expanduser()
    if not env_file.is_absolute():
        env_file = secrets_root / env_file
    try:
        env_file = env_file.resolve(strict=True)
        env_file.relative_to(secrets_root)
        info = env_file.stat()
    except (OSError, ValueError):
        raise BrowserAuthProfileError(
            "browser auth credential file must be inside the Hermes secrets directory"
        ) from None
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise BrowserAuthProfileError(
            "browser auth credential file must be a private regular file"
        )
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise BrowserAuthProfileError(
            "browser auth credential file must be owned by the Hermes user"
        )

    username_env = str(raw.get("username_env") or "PID_QA_USERNAME").strip()
    password_env = str(raw.get("password_env") or "PID_QA_PASSWORD").strip()
    if not _ENV_KEY_RE.fullmatch(username_env) or not _ENV_KEY_RE.fullmatch(
        password_env
    ):
        raise BrowserAuthProfileError(
            "browser auth profile has invalid credential keys"
        )
    try:
        timeout_s = float(raw.get("timeout_s", 30.0))
    except (TypeError, ValueError):
        timeout_s = 30.0
    timeout_s = max(1.0, min(timeout_s, 30.0))
    return BrowserAuthProfile(
        name=name,
        origins=origins,
        origin_patterns=origin_patterns,
        env_file=env_file,
        username_env=username_env,
        password_env=password_env,
        username_selector=_selector(raw.get("username_selector"), "username selector"),
        password_selector=_selector(raw.get("password_selector"), "password selector"),
        submit_selector=_selector(raw.get("submit_selector"), "submit selector"),
        success_selector=_selector(raw.get("success_selector"), "success selector"),
        timeout_s=timeout_s,
    )


def select_browser_auth_profile(
    origin: str,
    *,
    requested_name: str = "",
    config: Any = None,
) -> BrowserAuthProfile:
    normalized_origin = _normalize_origin(origin)
    profiles = _browser_config(config).get("auth_profiles")
    if not isinstance(profiles, dict):
        raise BrowserAuthProfileError(
            "no browser authentication profiles are configured"
        )
    requested = str(requested_name or "").strip()
    candidates: list[BrowserAuthProfile] = []
    for raw_name, raw in profiles.items():
        name = str(raw_name or "").strip()
        if requested and name != requested:
            continue
        try:
            profile = _profile_from_config(name, raw)
        except BrowserAuthProfileError:
            if requested:
                raise
            continue
        if _profile_matches_origin(profile, normalized_origin):
            candidates.append(profile)
    if not candidates:
        raise BrowserAuthProfileError(
            "no browser authentication profile matches the current origin"
        )
    if len(candidates) > 1 and not requested:
        names = ", ".join(profile.name for profile in candidates)
        raise BrowserAuthProfileError(
            f"multiple browser authentication profiles match; choose one of: {names}"
        )
    return candidates[0]


def load_browser_auth_credentials(profile: BrowserAuthProfile) -> tuple[str, str]:
    values: dict[str, str] = {}
    try:
        source = profile.env_file.read_text(encoding="utf-8")
    except OSError:
        raise BrowserAuthProfileError(
            "browser auth credential file could not be read"
        ) from None
    wanted = {profile.username_env, profile.password_env}
    for raw_line in source.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, separator, raw_value = line.partition("=")
        key = key.strip()
        if not separator or key not in wanted or key in values:
            continue
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    username = values.get(profile.username_env, "")
    password = values.get(profile.password_env, "")
    if not username or not password:
        raise BrowserAuthProfileError("browser auth credential pair is incomplete")
    return username, password


__all__ = [
    "BrowserAuthProfile",
    "BrowserAuthProfileError",
    "configured_browser_auth_profile_names",
    "load_browser_auth_credentials",
    "matching_browser_auth_profile_names",
    "select_browser_auth_profile",
]
