"""Shell-free structural policy for terminal observation in read-only mode."""

from __future__ import annotations

import os
import shlex
from pathlib import Path
from typing import Any


_SHELL_CONTROL_CHARS = frozenset("\n\r;|&<>`")
_NO_ARGUMENT_COMMANDS = frozenset(
    {"date", "hostname", "id", "pwd", "true", "false", "uptime", "whoami"}
)
_READ_ARGUMENT_COMMANDS = frozenset({"df", "pgrep", "ps", "uname"})


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
        return "shell operators, substitutions, pipes, and redirections are unavailable"
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return "command could not be parsed safely"
    if not tokens:
        return "command must contain an executable"
    if "=" in tokens[0] or os.path.isabs(tokens[0]):
        return "environment assignments and absolute executables are unavailable"
    base = Path(tokens[0]).name
    if base in _NO_ARGUMENT_COMMANDS:
        return True if len(tokens) == 1 else f"{base} arguments are unavailable in read-only mode"
    if base in _READ_ARGUMENT_COMMANDS:
        return True
    if base in {"python", "python3", "node", "npm", "pnpm", "uv"} and len(tokens) == 2:
        return True if tokens[1] in {"--version", "-V", "-v"} else "only version inspection is allowed"
    return (
        "terminal is limited to bounded process/system inspection; use read_file, "
        "search_files, session_search, or read_only_verify for broader observation"
    )
