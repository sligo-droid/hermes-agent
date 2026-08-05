"""
Session-scoped context variables for the Hermes gateway.

Replaces the previous ``os.environ``-based session state
(``HERMES_SESSION_PLATFORM``, ``HERMES_SESSION_CHAT_ID``, etc.) with
Python's ``contextvars.ContextVar``.

**Why this matters**

The gateway processes messages concurrently via ``asyncio``.  When two
messages arrive at the same time the old code did:

    os.environ["HERMES_SESSION_THREAD_ID"] = str(context.source.thread_id)

Because ``os.environ`` is *process-global*, Message A's value was
silently overwritten by Message B before Message A's agent finished
running.  Background-task notifications and tool calls therefore routed
to the wrong thread.

``contextvars.ContextVar`` values are *task-local*: each ``asyncio``
task (and any ``run_in_executor`` thread it spawns) gets its own copy,
so concurrent messages never interfere.

**Backward compatibility**

The public helper ``get_session_env(name, default="")`` mirrors the old
``os.getenv("HERMES_SESSION_*", ...)`` calls.  Existing tool code only
needs to replace the import + call site:

    # before
    import os
    platform = os.getenv("HERMES_SESSION_PLATFORM", "")

    # after
    from gateway.session_context import get_session_env
    platform = get_session_env("HERMES_SESSION_PLATFORM", "")
"""

from contextvars import ContextVar, Token
from typing import Any

# Sentinel to distinguish "never set in this context" from "explicitly set to empty".
# When a contextvar holds _UNSET, we fall back to os.environ (CLI/cron compat).
# When it holds "" (after clear_session_vars resets it), we return "" — no fallback.
_UNSET: Any = object()

# Process-level latch: once any code in this process binds session ContextVars,
# subprocess-env builders must treat ContextVars as authoritative and strip
# stale process-global HERMES_SESSION_* values when the ContextVar is _UNSET.
# Pure one-shot CLI paths that never call set_session_vars keep the os.environ
# fallback for backwards compatibility.
_session_context_engaged: bool = False


def session_context_engaged() -> bool:
    return _session_context_engaged


# ---------------------------------------------------------------------------
# Per-task session variables
# ---------------------------------------------------------------------------

_SESSION_PLATFORM: ContextVar = ContextVar("HERMES_SESSION_PLATFORM", default=_UNSET)
_SESSION_SOURCE: ContextVar = ContextVar("HERMES_SESSION_SOURCE", default=_UNSET)
_SESSION_CHAT_ID: ContextVar = ContextVar("HERMES_SESSION_CHAT_ID", default=_UNSET)
_SESSION_CHAT_NAME: ContextVar = ContextVar("HERMES_SESSION_CHAT_NAME", default=_UNSET)
_SESSION_THREAD_ID: ContextVar = ContextVar("HERMES_SESSION_THREAD_ID", default=_UNSET)
_SESSION_USER_ID: ContextVar = ContextVar("HERMES_SESSION_USER_ID", default=_UNSET)
_SESSION_USER_NAME: ContextVar = ContextVar("HERMES_SESSION_USER_NAME", default=_UNSET)
_SESSION_KEY: ContextVar = ContextVar("HERMES_SESSION_KEY", default=_UNSET)
_SESSION_ID: ContextVar = ContextVar("HERMES_SESSION_ID", default=_UNSET)
_SESSION_UI_SESSION_ID: ContextVar = ContextVar("HERMES_UI_SESSION_ID", default=_UNSET)
_SESSION_CWD: ContextVar = ContextVar("HERMES_SESSION_CWD", default=_UNSET)
_SESSION_PROFILE: ContextVar = ContextVar("HERMES_SESSION_PROFILE", default=_UNSET)
_SESSION_ASYNC_DELIVERY: ContextVar = ContextVar("HERMES_SESSION_ASYNC_DELIVERY", default=_UNSET)
_CRON_EXECUTION: ContextVar = ContextVar("HERMES_CRON_SESSION", default=_UNSET)
_PROJECT_KEY: ContextVar = ContextVar("HERMES_PROJECT_KEY", default=_UNSET)
_PROJECT_PATH: ContextVar = ContextVar("HERMES_PROJECT_PATH", default=_UNSET)
_PROJECT_NAME: ContextVar = ContextVar("HERMES_PROJECT_NAME", default=_UNSET)
_PROJECT_GITHUB_URL: ContextVar = ContextVar("HERMES_PROJECT_GITHUB_URL", default=_UNSET)
_PROJECT_CHANNEL_ID: ContextVar = ContextVar("HERMES_PROJECT_CHANNEL_ID", default=_UNSET)
_PROJECT_INSPECTION_CANDIDATES: ContextVar = ContextVar(
    "HERMES_PROJECT_INSPECTION_CANDIDATES", default=_UNSET
)
_SESSION_GUILD_ID: ContextVar = ContextVar("HERMES_SESSION_GUILD_ID", default=_UNSET)
_SESSION_PARENT_CHAT_ID: ContextVar = ContextVar("HERMES_SESSION_PARENT_CHAT_ID", default=_UNSET)
_KANBAN_DEFAULT_INTAKE: ContextVar = ContextVar("HERMES_KANBAN_DEFAULT_INTAKE", default=_UNSET)
_KANBAN_DEFAULT_INTAKE_ASSIGNEE: ContextVar = ContextVar("HERMES_KANBAN_DEFAULT_INTAKE_ASSIGNEE", default=_UNSET)
_KANBAN_NOTIFY_PROFILE: ContextVar = ContextVar("HERMES_KANBAN_NOTIFY_PROFILE", default=_UNSET)
# ID of the message that triggered the current turn. Used as a reply anchor
# so background-process notifications stay inside the originating Telegram
# private-chat topic (those lanes route only with thread id + reply anchor).
_SESSION_MESSAGE_ID: ContextVar = ContextVar("HERMES_SESSION_MESSAGE_ID", default=_UNSET)
_DISCORD_ACTION_ESCALATION_ALLOWED: ContextVar = ContextVar(
    "HERMES_DISCORD_ACTION_ESCALATION_ALLOWED", default=_UNSET
)
# Trusted gateway-only provenance for tools that start detached work.  This is
# intentionally excluded from _VAR_MAP: it has no os.environ compatibility
# fallback and must never be copied into a subprocess environment.
_TRUSTED_DISCORD_WORK_ITEM_ID: ContextVar = ContextVar(
    "trusted_discord_work_item_id", default=""
)

# Cron auto-delivery vars — set per-job in run_job() so concurrent jobs
# don't clobber each other's delivery targets.
_CRON_AUTO_DELIVER_PLATFORM: ContextVar = ContextVar("HERMES_CRON_AUTO_DELIVER_PLATFORM", default=_UNSET)
_CRON_AUTO_DELIVER_CHAT_ID: ContextVar = ContextVar("HERMES_CRON_AUTO_DELIVER_CHAT_ID", default=_UNSET)
_CRON_AUTO_DELIVER_THREAD_ID: ContextVar = ContextVar("HERMES_CRON_AUTO_DELIVER_THREAD_ID", default=_UNSET)

_VAR_MAP = {
    "HERMES_SESSION_PLATFORM": _SESSION_PLATFORM,
    "HERMES_SESSION_SOURCE": _SESSION_SOURCE,
    "HERMES_SESSION_CHAT_ID": _SESSION_CHAT_ID,
    "HERMES_SESSION_CHAT_NAME": _SESSION_CHAT_NAME,
    "HERMES_SESSION_THREAD_ID": _SESSION_THREAD_ID,
    "HERMES_SESSION_USER_ID": _SESSION_USER_ID,
    "HERMES_SESSION_USER_NAME": _SESSION_USER_NAME,
    "HERMES_SESSION_KEY": _SESSION_KEY,
    "HERMES_SESSION_ID": _SESSION_ID,
    "HERMES_UI_SESSION_ID": _SESSION_UI_SESSION_ID,
    "HERMES_SESSION_CWD": _SESSION_CWD,
    "HERMES_SESSION_PROFILE": _SESSION_PROFILE,
    "HERMES_PROJECT_KEY": _PROJECT_KEY,
    "HERMES_PROJECT_PATH": _PROJECT_PATH,
    "HERMES_PROJECT_NAME": _PROJECT_NAME,
    "HERMES_PROJECT_GITHUB_URL": _PROJECT_GITHUB_URL,
    "HERMES_PROJECT_CHANNEL_ID": _PROJECT_CHANNEL_ID,
    "HERMES_PROJECT_INSPECTION_CANDIDATES": _PROJECT_INSPECTION_CANDIDATES,
    "HERMES_SESSION_GUILD_ID": _SESSION_GUILD_ID,
    "HERMES_SESSION_PARENT_CHAT_ID": _SESSION_PARENT_CHAT_ID,
    "HERMES_KANBAN_DEFAULT_INTAKE": _KANBAN_DEFAULT_INTAKE,
    "HERMES_KANBAN_DEFAULT_INTAKE_ASSIGNEE": _KANBAN_DEFAULT_INTAKE_ASSIGNEE,
    "HERMES_KANBAN_NOTIFY_PROFILE": _KANBAN_NOTIFY_PROFILE,
    "HERMES_SESSION_MESSAGE_ID": _SESSION_MESSAGE_ID,
    "HERMES_DISCORD_ACTION_ESCALATION_ALLOWED": _DISCORD_ACTION_ESCALATION_ALLOWED,
    "HERMES_CRON_SESSION": _CRON_EXECUTION,
    "HERMES_CRON_AUTO_DELIVER_PLATFORM": _CRON_AUTO_DELIVER_PLATFORM,
    "HERMES_CRON_AUTO_DELIVER_CHAT_ID": _CRON_AUTO_DELIVER_CHAT_ID,
    "HERMES_CRON_AUTO_DELIVER_THREAD_ID": _CRON_AUTO_DELIVER_THREAD_ID,
}


def set_cron_execution(active: bool = True) -> Token:
    """Bind cron identity to the current task/thread and return a reset token."""
    return _CRON_EXECUTION.set("1" if active else "")


def reset_cron_execution(token: Token) -> None:
    """Restore the cron identity that preceded :func:`set_cron_execution`."""
    _CRON_EXECUTION.reset(token)


def is_cron_execution() -> bool:
    """Whether the current context is a cron run, with legacy env fallback."""
    from utils import env_var_enabled, is_truthy_value

    value = _CRON_EXECUTION.get()
    if value is not _UNSET:
        return is_truthy_value(value)
    return env_var_enabled("HERMES_CRON_SESSION")


def set_current_session_id(session_id: str) -> None:
    """Synchronize ``HERMES_SESSION_ID`` across ContextVar and ``os.environ``.

    Long-lived single-process entrypoints like the CLI can rotate sessions via
    ``/new``, ``/resume``, ``/branch``, or compression splits without
    reconstructing the entire agent. Tools still consult
    ``get_session_env("HERMES_SESSION_ID")`` with an ``os.environ`` fallback,
    so both storage paths must move together when the active session changes.
    """
    import os

    os.environ["HERMES_SESSION_ID"] = session_id
    _SESSION_ID.set(session_id)


def bind_project_inspection_candidates(candidates: Any) -> Token:
    """Bind a safe serialized candidate list to the current task."""
    from hermes_cli.project_inspection import serialize_project_inspection_candidates

    return _PROJECT_INSPECTION_CANDIDATES.set(
        serialize_project_inspection_candidates(candidates)
    )


def reset_project_inspection_candidates(token: Token) -> None:
    """Restore the candidate binding that preceded a task-local bind."""
    _PROJECT_INSPECTION_CANDIDATES.reset(token)


def bind_discord_action_escalation_allowed(allowed: bool) -> Token:
    """Bind the Discord action-handoff capability for one agent turn."""
    return _DISCORD_ACTION_ESCALATION_ALLOWED.set("1" if allowed else "")


def reset_discord_action_escalation_allowed(token: Token) -> None:
    """Restore the action-handoff capability from before the agent turn."""
    _DISCORD_ACTION_ESCALATION_ALLOWED.reset(token)


def set_session_vars(
    platform: str = "",
    source: str = "",
    chat_id: str = "",
    chat_name: str = "",
    thread_id: str = "",
    user_id: str = "",
    user_name: str = "",
    session_key: str = "",
    session_id: str = "",
    ui_session_id: str = "",
    session_cwd: str = "",
    cwd: str = "",
    profile: str = "",
    async_delivery: bool = True,
    project_key: str = "",
    project_path: str = "",
    project_name: str = "",
    project_github_url: str = "",
    project_channel_id: str = "",
    project_inspection_candidates: Any = None,
    guild_id: str = "",
    parent_chat_id: str = "",
    kanban_default_intake: str = "",
    kanban_default_intake_assignee: str = "",
    kanban_notify_profile: str = "",
    message_id: str = "",
    discord_action_escalation_allowed: str = "",
) -> list:
    """Set all session context variables and return reset tokens.

    Call ``clear_session_vars(tokens)`` in a ``finally`` block to restore
    the previous values when the handler exits.

    Returns a list of ``Token`` objects (one per variable) that can be
    passed to ``clear_session_vars``.
    """
    global _session_context_engaged
    _session_context_engaged = True
    effective_cwd = cwd or session_cwd

    tokens = [
        _SESSION_PLATFORM.set(platform),
        _SESSION_SOURCE.set(source),
        _SESSION_CHAT_ID.set(chat_id),
        _SESSION_CHAT_NAME.set(chat_name),
        _SESSION_THREAD_ID.set(thread_id),
        _SESSION_USER_ID.set(user_id),
        _SESSION_USER_NAME.set(user_name),
        _SESSION_KEY.set(session_key),
        _SESSION_ID.set(session_id),
        _SESSION_UI_SESSION_ID.set(ui_session_id),
        _SESSION_CWD.set(effective_cwd),
        _SESSION_PROFILE.set(profile),
        _SESSION_ASYNC_DELIVERY.set(bool(async_delivery)),
        _PROJECT_KEY.set(project_key),
        _PROJECT_PATH.set(project_path),
        _PROJECT_NAME.set(project_name),
        _PROJECT_GITHUB_URL.set(project_github_url),
        _PROJECT_CHANNEL_ID.set(project_channel_id),
        bind_project_inspection_candidates(project_inspection_candidates),
        _SESSION_GUILD_ID.set(guild_id),
        _SESSION_PARENT_CHAT_ID.set(parent_chat_id),
        _KANBAN_DEFAULT_INTAKE.set(kanban_default_intake),
        _KANBAN_DEFAULT_INTAKE_ASSIGNEE.set(kanban_default_intake_assignee),
        _KANBAN_NOTIFY_PROFILE.set(kanban_notify_profile),
        _SESSION_MESSAGE_ID.set(message_id),
        _DISCORD_ACTION_ESCALATION_ALLOWED.set(discord_action_escalation_allowed),
    ]
    try:
        from agent.runtime_cwd import set_session_cwd

        set_session_cwd(effective_cwd)
    except Exception:
        pass
    return tokens


def clear_session_vars(tokens: list) -> None:
    """Mark session context variables as explicitly cleared.

    Sets all variables to ``""`` so that ``get_session_env`` returns an empty
    string instead of falling back to (potentially stale) ``os.environ``
    values.  The *tokens* argument is accepted for API compatibility with
    callers that saved the return value of ``set_session_vars``, but the
    actual clearing uses ``var.set("")`` rather than ``var.reset(token)``
    to ensure the "explicitly cleared" state is distinguishable from
    "never set" (which holds the ``_UNSET`` sentinel).
    """
    for var in (
        _SESSION_PLATFORM,
        _SESSION_SOURCE,
        _SESSION_CHAT_ID,
        _SESSION_CHAT_NAME,
        _SESSION_THREAD_ID,
        _SESSION_USER_ID,
        _SESSION_USER_NAME,
        _SESSION_KEY,
        _SESSION_ID,
        _SESSION_UI_SESSION_ID,
        _SESSION_CWD,
        _SESSION_PROFILE,
        _PROJECT_KEY,
        _PROJECT_PATH,
        _PROJECT_NAME,
        _PROJECT_GITHUB_URL,
        _PROJECT_CHANNEL_ID,
        _PROJECT_INSPECTION_CANDIDATES,
        _SESSION_GUILD_ID,
        _SESSION_PARENT_CHAT_ID,
        _KANBAN_DEFAULT_INTAKE,
        _KANBAN_DEFAULT_INTAKE_ASSIGNEE,
        _KANBAN_NOTIFY_PROFILE,
        _SESSION_MESSAGE_ID,
        _DISCORD_ACTION_ESCALATION_ALLOWED,
        _TRUSTED_DISCORD_WORK_ITEM_ID,
    ):
        var.set("")
    _SESSION_ASYNC_DELIVERY.set(_UNSET)
    try:
        from agent.runtime_cwd import clear_session_cwd

        clear_session_cwd()
    except Exception:
        pass


def reset_session_vars() -> None:
    """Reset session context variables to the unbound sentinel for this context."""
    for var in _VAR_MAP.values():
        var.set(_UNSET)
    _TRUSTED_DISCORD_WORK_ITEM_ID.set("")
    _SESSION_ASYNC_DELIVERY.set(_UNSET)
    try:
        from agent.runtime_cwd import clear_session_cwd

        clear_session_cwd()
    except Exception:
        pass


def get_session_env(name: str, default: str = "") -> str:
    """Read a session context variable by its legacy ``HERMES_SESSION_*`` name.

    Drop-in replacement for ``os.getenv("HERMES_SESSION_*", default)``.

    Resolution order:
    1. Context variable (set by the gateway for concurrency-safe access).
       If the variable was explicitly set (even to ``""``) via
       ``set_session_vars`` or ``clear_session_vars``, that value is
       returned — **no fallback to os.environ**.
    2. ``os.environ`` (only when the context variable was never set in
       this context — i.e. CLI, cron scheduler, and test processes that
       don't use ``set_session_vars`` at all).
    3. *default*
    """
    import os

    var = _VAR_MAP.get(name)
    if var is not None:
        value = var.get()
        if value is not _UNSET:
            return value
    # Fall back to os.environ for CLI, cron, and test compatibility
    return os.getenv(name, default)


def get_trusted_discord_work_item_id() -> str:
    """Return the gateway-bound Discord action owner for the current task.

    Unlike ``get_session_env()``, this provenance has no process-environment
    fallback.  Only the gateway turn binder supplies it, and non-action turns
    explicitly bind an empty value.
    """
    return str(_TRUSTED_DISCORD_WORK_ITEM_ID.get() or "")


def get_trusted_project_key() -> str:
    """Return the task-local project mapping without an environment fallback.

    Gateway turns bind ``_PROJECT_KEY`` through :func:`set_session_vars`. A
    process-level ``HERMES_PROJECT_KEY`` is intentionally ignored here so
    authorization-sensitive tools cannot be retargeted by inherited env.
    """
    value = _PROJECT_KEY.get()
    if value is _UNSET:
        return ""
    return str(value or "")


def _bind_trusted_discord_work_item_id(work_item_id: str = "") -> None:
    """Bind provenance selected by the gateway's trusted turn router."""
    _TRUSTED_DISCORD_WORK_ITEM_ID.set(str(work_item_id or ""))


def declare_stateless_channel() -> None:
    """Declare that this session cannot receive an async background completion.

    Binds only the delivery capability, leaving every other session var unset.
    Use this instead of ``set_session_vars(async_delivery=False)`` on a pure
    single-process runner: ``set_session_vars`` also latches
    ``_session_context_engaged`` (see above), which switches the subprocess
    env bridge from "os.environ fallback" to "ContextVar-authoritative, strip on
    _UNSET" in ``tools/environments/local.py``. A one-shot CLI that never engages
    the session-context system must not flip that latch as a side effect of
    declaring a capability.

    Callers that already build a full session context (cron's ``run_job``) get
    the same state by passing ``async_delivery=False`` to ``set_session_vars``.

    A session that cannot take a late completion makes ``delegate_task`` fall
    through to its existing inline/synchronous path, so subagent results are
    returned within the turn instead of being dispatched to a channel that will
    never deliver them.

    See NousResearch/hermes-agent#53027 and #63142.
    """
    _SESSION_ASYNC_DELIVERY.set(False)


def async_delivery_supported() -> bool:
    """Whether the current session can deliver background completions later."""
    value = _SESSION_ASYNC_DELIVERY.get()
    if value is _UNSET:
        return True
    return bool(value)
