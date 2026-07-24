"""Shell-free structural policy for terminal observation in read-only mode."""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path
from typing import Any


_SHELL_CONTROL_CHARS = frozenset("\n\r;|&<>`$*?[]{}")
_NO_ARGUMENT_COMMANDS = frozenset(
    {"date", "hostname", "id", "pwd", "true", "false", "uptime", "whoami"}
)
_UNAME_FLAGS = frozenset(
    {
        "-a",
        "--all",
        "-s",
        "--kernel-name",
        "-n",
        "--nodename",
        "-r",
        "--kernel-release",
        "-v",
        "--kernel-version",
        "-m",
        "--machine",
        "-p",
        "--processor",
        "-i",
        "--hardware-platform",
        "-o",
        "--operating-system",
    }
)
_DF_FLAGS = frozenset(
    {
        "-h",
        "--human-readable",
        "-H",
        "--si",
        "-i",
        "--inodes",
        "-P",
        "--portability",
        "-T",
        "--print-type",
        "-l",
        "--local",
        "--total",
    }
)
_PGREP_BOOLEAN_FLAGS = frozenset(
    {
        "-a",
        "--list-full",
        "-l",
        "--list-name",
        "-f",
        "--full",
        "-x",
        "--exact",
        "-n",
        "--newest",
        "-o",
        "--oldest",
        "-v",
        "--inverse",
        "-c",
        "--count",
        "-i",
        "--ignore-case",
        "--ignore-ancestors",
    }
)
_PGREP_VALUE_FLAGS = {
    "-u": re.compile(r"^[A-Za-z0-9_.-]+(?:,[A-Za-z0-9_.-]+)*$"),
    "--euid": re.compile(r"^[A-Za-z0-9_.-]+(?:,[A-Za-z0-9_.-]+)*$"),
    "-U": re.compile(r"^[A-Za-z0-9_.-]+(?:,[A-Za-z0-9_.-]+)*$"),
    "--uid": re.compile(r"^[A-Za-z0-9_.-]+(?:,[A-Za-z0-9_.-]+)*$"),
    "-G": re.compile(r"^[A-Za-z0-9_.-]+(?:,[A-Za-z0-9_.-]+)*$"),
    "--group": re.compile(r"^[A-Za-z0-9_.-]+(?:,[A-Za-z0-9_.-]+)*$"),
    "-g": re.compile(r"^-?\d+(?:,-?\d+)*$"),
    "--pgroup": re.compile(r"^-?\d+(?:,-?\d+)*$"),
    "-P": re.compile(r"^\d+(?:,\d+)*$"),
    "--parent": re.compile(r"^\d+(?:,\d+)*$"),
    "-s": re.compile(r"^-?\d+(?:,-?\d+)*$"),
    "--session": re.compile(r"^-?\d+(?:,-?\d+)*$"),
    "-t": re.compile(r"^[A-Za-z0-9,./_-]+$"),
    "--terminal": re.compile(r"^[A-Za-z0-9,./_-]+$"),
    "-r": re.compile(r"^[RSDZTtWXIKP]+$"),
    "--runstates": re.compile(r"^[RSDZTtWXIKP]+$"),
    "-O": re.compile(r"^\d+$"),
    "--older": re.compile(r"^\d+$"),
}
_PS_FIXED_FORMS = frozenset(
    {
        (),
        ("-A",),
        ("-e",),
        ("-ef",),
        ("-f",),
        ("a",),
        ("ax",),
        ("aux",),
        ("-aux",),
        ("x",),
    }
)
_PS_SAFE_COLUMNS = frozenset(
    {
        "pid",
        "ppid",
        "pgid",
        "sid",
        "user",
        "uid",
        "group",
        "gid",
        "stat",
        "state",
        "etime",
        "etimes",
        "lstart",
        "time",
        "pcpu",
        "pmem",
        "rss",
        "vsz",
        "tty",
        "comm",
        "command",
        "args",
    }
)
_PS_SAFE_SORT_KEYS = frozenset(
    {"pid", "ppid", "pgid", "sid", "uid", "gid", "etime", "etimes", "pcpu", "pmem", "rss", "vsz"}
)


def _safe_path_operand(value: str) -> bool:
    return bool(value and not value.startswith("~") and "\x00" not in value)


def _check_uname(tokens: list[str]) -> bool | str:
    if len(tokens) == 1:
        return True
    for flag in tokens[1:]:
        if flag in _UNAME_FLAGS:
            continue
        if re.fullmatch(r"-[asnrvmpio]+", flag):
            continue
        return f"uname option {flag!r} is unavailable in read-only mode"
    return True


def _check_df(tokens: list[str]) -> bool | str:
    paths = 0
    for token in tokens[1:]:
        if token.startswith("-"):
            if token not in _DF_FLAGS:
                return f"df option {token!r} is unavailable in read-only mode"
            continue
        if not _safe_path_operand(token):
            return "df paths must be literal filesystem paths"
        paths += 1
        if paths > 8:
            return "df is limited to eight explicit paths"
    return True


def _check_pgrep(tokens: list[str]) -> bool | str:
    index = 1
    pattern: str | None = None
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            index += 1
            if index >= len(tokens) or pattern is not None:
                return "pgrep requires exactly one literal pattern"
            pattern = tokens[index]
            index += 1
            if index != len(tokens):
                return "pgrep accepts one pattern only"
            break
        if token in _PGREP_BOOLEAN_FLAGS:
            index += 1
            continue
        if re.fullmatch(r"-[alfxnovci]+", token):
            index += 1
            continue
        matcher = _PGREP_VALUE_FLAGS.get(token)
        if matcher is not None:
            index += 1
            if index >= len(tokens) or not matcher.fullmatch(tokens[index]):
                return f"pgrep option {token!r} requires a bounded literal value"
            index += 1
            continue
        matched_long_value = False
        for flag, candidate_matcher in _PGREP_VALUE_FLAGS.items():
            if not flag.startswith("--") or not token.startswith(flag + "="):
                continue
            value = token.split("=", 1)[1]
            if not candidate_matcher.fullmatch(value):
                return f"pgrep option {flag!r} requires a bounded literal value"
            matched_long_value = True
            break
        if matched_long_value:
            index += 1
            continue
        if token.startswith("-"):
            return f"pgrep option {token!r} is unavailable in read-only mode"
        if pattern is not None:
            return "pgrep accepts one pattern only"
        pattern = token
        index += 1
    if not pattern or len(pattern) > 256:
        return "pgrep requires one pattern of at most 256 characters"
    return True


def _safe_ps_columns(value: str) -> bool:
    columns = [part.strip().split("=", 1)[0].lower() for part in value.split(",")]
    return bool(columns) and len(columns) <= 16 and all(column in _PS_SAFE_COLUMNS for column in columns)


def _safe_ps_sort(value: str) -> bool:
    keys = [part.strip().lstrip("+-").lower() for part in value.split(",")]
    return bool(keys) and len(keys) <= 8 and all(key in _PS_SAFE_SORT_KEYS for key in keys)


def _check_ps(tokens: list[str]) -> bool | str:
    tail = tuple(tokens[1:])
    if tail in _PS_FIXED_FORMS:
        return True
    index = 1
    saw_selector = False
    while index < len(tokens):
        token = tokens[index]
        if token in {"-A", "-e", "a", "ax", "x", "-f", "--forest", "--no-headers"}:
            saw_selector = True
            index += 1
            continue
        if token in {"-p", "--pid", "--ppid"}:
            index += 1
            if index >= len(tokens) or not re.fullmatch(r"\d+(?:,\d+)*", tokens[index]):
                return f"ps option {token!r} requires a numeric PID list"
            saw_selector = True
            index += 1
            continue
        if token in {"-o", "--format"}:
            index += 1
            if index >= len(tokens) or not _safe_ps_columns(tokens[index]):
                return "ps custom output is limited to approved non-environment columns"
            index += 1
            continue
        if token in {"-eo", "-Ao"}:
            saw_selector = True
            index += 1
            if index >= len(tokens) or not _safe_ps_columns(tokens[index]):
                return "ps custom output is limited to approved non-environment columns"
            index += 1
            continue
        if token == "--sort":
            index += 1
            if index >= len(tokens) or not _safe_ps_sort(tokens[index]):
                return "ps sorting is limited to approved numeric process fields"
            index += 1
            continue
        if token.startswith("--sort="):
            if not _safe_ps_sort(token.split("=", 1)[1]):
                return "ps sorting is limited to approved numeric process fields"
            index += 1
            continue
        if token.startswith("-") or re.fullmatch(r"[A-Za-z]+", token):
            return f"ps option {token!r} is unavailable in read-only mode"
        return "ps accepts only explicit bounded selectors and output fields"
    return True if saw_selector else "ps requires an approved inspection form"


def read_only_terminal_check(args: dict[str, Any]) -> bool | str:
    """Approve one foreground observation command after structural parsing."""

    if args.get("background") or args.get("pty") or args.get("notify_on_complete"):
        return "background, PTY, and completion-notification terminal modes are unavailable"
    if args.get("watch_patterns"):
        return "watch patterns are unavailable"
    command = args.get("command")
    if not isinstance(command, str) or not command.strip():
        return "command must be a non-empty string"
    if any(char in command for char in _SHELL_CONTROL_CHARS) or "$(" in command:
        return "shell operators, expansions, substitutions, pipes, and redirections are unavailable"
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return "command could not be parsed safely"
    if not tokens:
        return "command must contain an executable"
    if "=" in tokens[0] or os.path.isabs(tokens[0]):
        return "environment assignments and absolute executables are unavailable"
    if tokens[0] != Path(tokens[0]).name:
        return "relative executable paths are unavailable in read-only mode"
    base = Path(tokens[0]).name
    if base in _NO_ARGUMENT_COMMANDS:
        return True if len(tokens) == 1 else f"{base} arguments are unavailable in read-only mode"
    if base == "uname":
        return _check_uname(tokens)
    if base == "df":
        return _check_df(tokens)
    if base == "pgrep":
        return _check_pgrep(tokens)
    if base == "ps":
        return _check_ps(tokens)
    if base in {"python", "python3", "node", "npm", "pnpm", "uv"} and len(tokens) == 2:
        return True if tokens[1] in {"--version", "-V", "-v"} else "only version inspection is allowed"
    return (
        "terminal is limited to bounded process/system inspection; use read_file, "
        "search_files, session_search, or read_only_verify for broader observation"
    )
