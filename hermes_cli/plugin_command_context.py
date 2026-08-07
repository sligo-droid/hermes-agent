"""Trusted post-authorization context for one-argument plugin commands."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterator


@dataclass(frozen=True, slots=True)
class PluginCommandContext:
    event: Any
    canonical_command: str
    raw_args: str
    authorization_passed: bool
    internal: bool


_CURRENT: ContextVar[PluginCommandContext | None] = ContextVar(
    "hermes_plugin_command_context", default=None
)


def get_plugin_command_context() -> PluginCommandContext | None:
    return _CURRENT.get()


@contextmanager
def bind_plugin_command_context(
    *, event: Any, canonical_command: str, raw_args: str
) -> Iterator[PluginCommandContext]:
    context = PluginCommandContext(
        event=event,
        canonical_command=str(canonical_command),
        raw_args=str(raw_args),
        authorization_passed=True,
        internal=bool(getattr(event, "internal", False)),
    )
    token = _CURRENT.set(context)
    try:
        yield context
    finally:
        _CURRENT.reset(token)


__all__ = ["PluginCommandContext", "get_plugin_command_context"]
