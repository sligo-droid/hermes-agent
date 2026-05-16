"""Terminal notification escape sequences for interactive clients."""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Mapping
from typing import TextIO

ESC = "\x1b"
BEL = "\x07"
ST = ESC + "\\"

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _sanitize_osc_field(value: str) -> str:
    return _CONTROL_RE.sub(" ", value).replace(";", ",").strip()


def wrap_for_multiplexer(sequence: str, env: Mapping[str, str] | None = None) -> str:
    env = env if env is not None else os.environ

    if env.get("TMUX"):
        return f"{ESC}Ptmux;{sequence.replace(ESC, ESC + ESC)}{ST}"

    if env.get("STY"):
        return f"{ESC}P{sequence}{ST}"

    return sequence


def osc777_notify(title: str = "Hermes", message: str = "Response complete") -> str:
    safe_title = _sanitize_osc_field(title) or "Hermes"
    safe_message = _sanitize_osc_field(message) or "Response complete"

    return f"{ESC}]777;notify;{safe_title};{safe_message}{BEL}"


def emit_completion_notification(
    stream: TextIO | None = None,
    *,
    enabled: bool = True,
    env: Mapping[str, str] | None = None,
) -> bool:
    stream = stream if stream is not None else sys.stdout

    if not enabled or not getattr(stream, "isatty", lambda: False)():
        return False

    stream.write(wrap_for_multiplexer(osc777_notify(), env))
    stream.flush()

    return True
