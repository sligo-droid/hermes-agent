"""
Cron job scheduler - executes due jobs.

Provides tick() which checks for due jobs and runs them. The gateway
calls this every 60 seconds from a background thread.

Uses a file-based lock (~/.hermes/cron/.tick.lock) so only one tick
runs at a time if multiple processes overlap.
"""

import asyncio
import concurrent.futures
import contextvars
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import atexit
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime

# fcntl is Unix-only; on Windows use msvcrt for file locking
try:
    import fcntl
except ImportError:
    fcntl = None
    try:
        import msvcrt
    except ImportError:
        msvcrt = None
from pathlib import Path
from typing import Any, List, Optional

# Add parent directory to path for imports BEFORE repo-level imports.
# Without this, standalone invocations (e.g. after `hermes update` reloads
# the module) fail with ModuleNotFoundError for hermes_time et al.
sys.path.insert(0, str(Path(__file__).parent.parent))

from hermes_constants import get_hermes_home
from hermes_cli._subprocess_compat import windows_hide_flags
from hermes_cli.config import load_config, _expand_env_vars
from hermes_cli.codex_auth_incidents import redact as redact_auth_incident_text
from hermes_cli.codex_auth_incidents import summarize_failure_text
from hermes_cli.fallback_config import get_fallback_chain
from hermes_time import now as _hermes_now

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CronAuthBlockedFailure:
    status: str
    provider_class: str
    failure_code: str
    summary: str


def _set_cron_session_title(session_db, session_id, base_title):
    """Robustly title a finished cron session before it is closed.

    Centralizes the title write so the cron finally block can guarantee a
    non-blank, unique title is persisted before end_session()/close() tear
    the connection down (issues #50535, #50536, #50537):

    - #50535: never leaves the session blank. base_title already carries a
      cron-id fallback for nameless jobs; this also guards a failed write.
    - #50537: a duplicate title makes set_session_title raise ValueError (the
      unique-title index). Recover by appending a #N suffix via
      get_next_title_in_lineage() when supported, instead of swallowing the
      error and ending up untitled. If lineage dedup is unavailable, raise.
    - #50536: this runs synchronously in the cron finally block ahead of the
      session close, so no in-flight title write can race the close.

    Returns the title actually persisted, or None if nothing could be set.
    """
    if not session_db or not session_id:
        return None
    title = (base_title or "").strip()
    if not title:
        return None
    try:
        session_db.set_session_title(session_id, title)
        return title
    except ValueError:
        # Title collision against the unique-title index. Fall back to the
        # next title in the lineage (base #2, base #3, ...) when supported.
        next_title_fn = getattr(session_db, "get_next_title_in_lineage", None)
        if next_title_fn is None:
            raise
        deduped = next_title_fn(title)
        if not deduped or deduped == title:
            raise
        session_db.set_session_title(session_id, deduped)
        return deduped


def _summarize_cron_failure_for_delivery(job: dict, error: str | None) -> str:
    """Return a compact one-line failure message for chat delivery.

    Full details stay in the cron output directory and the logs. Chat should
    show the operator what broke without dumping provider JSON, retry noise, or
    stack traces into the delivery channel.
    """
    job_name = job.get("name") or job.get("id") or "cron job"
    text = (error or "unknown error").strip()
    lower = text.lower()

    if "429" in text or "rate limit" in lower or "usage limit" in lower:
        reason = "rate limit"
        if "weekly usage limit" in lower:
            reason = "weekly usage limit"
        elif "quota" in lower:
            reason = "quota limit"
        return (
            f"⚠️ Cron '{job_name}' failed: provider {reason}. "
            "Fallback chain was exhausted or unavailable. "
            "Full details saved in cron output."
        )

    if "readtimeout" in lower or "timed out" in lower or "timeout" in lower:
        return (
            f"⚠️ Cron '{job_name}' failed: provider timeout. "
            "Fallback chain was exhausted or unavailable. "
            "Full details saved in cron output."
        )

    if re.search(r"authenticat|authoriz", lower) or re.search(r"\b(401|403)\b", text):
        return (
            f"⚠️ Cron '{job_name}' failed: provider authentication error. "
            "Full details saved in cron output."
        )

    cleaned = re.sub(
        r"^(RuntimeError|Exception|ValueError|HTTPStatusError):\s*",
        "", text[:2000],
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > 180:
        cleaned = cleaned[:177].rstrip() + "..."
    return f"⚠️ Cron '{job_name}' failed: {cleaned}"

_tick_state_lock = threading.Lock()
_active_tick_count = 0


def is_tick_running() -> bool:
    """Return true while this process is executing a scheduler tick."""
    with _tick_state_lock:
        return _active_tick_count > 0


def _set_tick_running(running: bool) -> None:
    global _active_tick_count
    with _tick_state_lock:
        if running:
            _active_tick_count += 1
        else:
            _active_tick_count = max(0, _active_tick_count - 1)


_GITHUB_PR_URL_RE = re.compile(
    r"https://github\.com/"
    r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/\d+"
    r"(?:[/?#][^\s<>()\[\]{}\"']*)?"
)


def _extract_github_pr_urls(text: str) -> list[str]:
    """Return unique GitHub PR URLs mentioned in a cron result."""
    urls: list[str] = []
    seen: set[str] = set()
    for match in _GITHUB_PR_URL_RE.finditer(str(text or "")):
        url = match.group(0).rstrip(".,;:")
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def _github_repo_url_from_pr_url(pr_url: str) -> Optional[str]:
    match = re.match(
        r"^(https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/\d+",
        str(pr_url or ""),
    )
    return match.group(1) if match else None


def _github_pr_number(pr_url: str) -> Optional[str]:
    match = re.search(r"/pull/(\d+)(?:\D|$)", str(pr_url or ""))
    return match.group(1) if match else None


def _cron_discord_feature_project_context(job: dict, pr_url: str) -> dict:
    context: dict = {}
    workdir = str(job.get("workdir") or "").strip()
    if workdir:
        context.update(
            {
                "project_name": Path(workdir).name,
                "project_path": workdir,
                "project_mapping_source": "cron",
                "project_mapping_resolved": True,
            }
        )
    repo_url = _github_repo_url_from_pr_url(pr_url)
    if repo_url:
        context["project_github_url"] = repo_url
    return context


def _cron_discord_feature_title(job: dict, pr_url: str) -> str:
    job_name = str(job.get("name") or job.get("id") or "Cron job").strip()
    pr_number = _github_pr_number(pr_url)
    if pr_number:
        return f"{job_name} PR #{pr_number}"
    return job_name


def _cron_discord_feature_initial_request(job: dict, pr_url: str) -> str:
    job_name = str(job.get("name") or job.get("id") or "Cron job").strip()
    return f"Cron job '{job_name}' shipped a change: {pr_url}"


def _cron_discord_feature_outcome(content: str, pr_url: str) -> str:
    lines = [line.strip() for line in str(content or "").splitlines() if line.strip()]
    selected = ""
    for index, line in enumerate(lines):
        if pr_url in line:
            window = lines[max(0, index - 2): index + 3]
            selected = " ".join(window)
            break
    if not selected and lines:
        selected = " ".join(lines[:3])
    if pr_url not in selected:
        selected = f"{selected} PR: {pr_url}".strip()
    return selected or f"PR: {pr_url}"


class CronPromptInjectionBlocked(Exception):
    """Raised by _build_job_prompt when the fully-assembled prompt trips the
    injection scanner. Caught in run_job so the operator sees a clean
    "job blocked" delivery instead of the scheduler crashing.

    Assembled-prompt scanning (including loaded skill content) plugs the
    gap from #3968: create-time scanning only covers the user-supplied
    prompt field; skill content loaded at runtime was never scanned, so a
    malicious skill could carry an injection payload that reached the
    non-interactive (auto-approve) cron agent.
    """


def _resolve_cron_disabled_toolsets(cfg: dict) -> list[str]:
    """Toolsets a cron-spawned agent must never receive.

    Three protected toolsets are always disabled in cron context:
      - ``cronjob`` — would let a cron-spawned agent schedule more cron jobs
      - ``messaging`` — interactive, needs a live gateway session
      - ``clarify`` — interactive, blocks waiting for user input

    User-level ``agent.disabled_toolsets`` from config.yaml is layered on top
    so per-job ``enabled_toolsets`` cannot bypass policy that applies to
    ordinary agent runs (#25752 — LLM-supplied enabled_toolsets was widening
    past config.yaml's denylist).
    """
    disabled = ["cronjob", "messaging", "clarify"]
    agent_cfg = (cfg or {}).get("agent") or {}
    user_disabled = agent_cfg.get("disabled_toolsets") or []
    for name in user_disabled:
        name = str(name).strip()
        if name and name not in disabled:
            disabled.append(name)
    return disabled


def _resolve_cron_enabled_toolsets(job: dict, cfg: dict) -> list[str] | None:
    """Resolve the toolset list for a cron job.

    Precedence:
    1. Per-job ``enabled_toolsets`` (set via ``cronjob`` tool on create/update).
       Keeps the agent's job-scoped toolset override intact — #6130.
    2. Per-platform ``hermes tools`` config for the ``cron`` platform.
       Mirrors gateway behavior (``_get_platform_tools(cfg, platform_key)``)
       so users can gate cron toolsets globally without recreating every job.
    3. ``None`` on any lookup failure — AIAgent loads the full default set
       (legacy behavior before this change, preserved as the safety net).

    _DEFAULT_OFF_TOOLSETS ({moa, homeassistant, rl}) are removed by
    ``_get_platform_tools`` for unconfigured platforms, so fresh installs
    get cron WITHOUT ``moa`` by default (issue reported by Norbert —
    surprise $4.63 run).
    """
    per_job = job.get("enabled_toolsets")
    if per_job:
        return per_job
    try:
        from hermes_cli.tools_config import _get_platform_tools  # lazy: avoid heavy import at cron module load
        return sorted(_get_platform_tools(cfg or {}, "cron"))
    except Exception as exc:
        logger.warning(
            "Cron toolset resolution failed, falling back to full default toolset: %s",
            exc,
        )
        return None

# Valid delivery platforms — used to validate user-supplied platform names
# in cron delivery targets, preventing env var enumeration via crafted names.
_KNOWN_DELIVERY_PLATFORMS = frozenset({
    "telegram", "discord", "slack", "whatsapp", "whatsapp_cloud", "signal",
    "matrix", "mattermost", "homeassistant", "dingtalk", "feishu",
    "wecom", "wecom_callback", "weixin", "sms", "email", "webhook", "bluebubbles",
    "qqbot", "yuanbao",
})

# Platforms that support a configured cron/notification home target, mapped to
# the environment variable used by gateway setup/runtime config.
_HOME_TARGET_ENV_VARS = {
    "matrix": "MATRIX_HOME_ROOM",
    "telegram": "TELEGRAM_HOME_CHANNEL",
    "discord": "DISCORD_HOME_CHANNEL",
    "slack": "SLACK_HOME_CHANNEL",
    "signal": "SIGNAL_HOME_CHANNEL",
    "mattermost": "MATTERMOST_HOME_CHANNEL",
    "sms": "SMS_HOME_CHANNEL",
    "email": "EMAIL_HOME_ADDRESS",
    "dingtalk": "DINGTALK_HOME_CHANNEL",
    "feishu": "FEISHU_HOME_CHANNEL",
    "wecom": "WECOM_HOME_CHANNEL",
    "weixin": "WEIXIN_HOME_CHANNEL",
    "bluebubbles": "BLUEBUBBLES_HOME_CHANNEL",
    "qqbot": "QQBOT_HOME_CHANNEL",
    "whatsapp": "WHATSAPP_HOME_CHANNEL",
    "whatsapp_cloud": "WHATSAPP_CLOUD_HOME_CHANNEL",
}

# Legacy env var names kept for back-compat.  Each entry is the current
# primary env var → the previous name.  _get_home_target_chat_id falls
# back to the legacy name if the primary is unset, so users who set the
# old name before the rename keep working until they migrate.
_LEGACY_HOME_TARGET_ENV_VARS = {
    "QQBOT_HOME_CHANNEL": "QQ_HOME_CHANNEL",
}

from cron.jobs import (
    advance_next_run,
    claim_dispatch,
    get_due_jobs,
    heartbeat_run_claim,
    load_jobs,
    mark_job_run,
    mark_job_terminal_success,
    mark_manual_run_finished,
    mark_manual_run_started,
    save_job_output,
    update_job_output,
)
from cron.executions import create_execution, finish_execution, mark_execution_running

# Sentinel: when a cron agent has nothing new to report, it can start its
# response with this marker to suppress delivery.  Output is still saved
# locally for audit.
SILENT_MARKER = "[SILENT]"

# Canonical silence tokens recognized in cron output.  Cron's contract is
# intentionally looser than the gateway's exact-whole-response rule: the cron
# system prompt *instructs* the agent to emit "[SILENT]", and real agents often
# bracket it with a short note or trailing newline.  We therefore suppress when
# a marker is the entire response OR appears as its own first/last line — but
# NOT when a token merely appears mid-sentence in a genuine report (e.g.
# "I considered staying [SILENT] but here is the summary…" must deliver).
_CRON_SILENCE_TOKENS = frozenset({"[SILENT]", "SILENT", "NO_REPLY", "NO REPLY"})


def _is_cron_silence_response(text: str) -> bool:
    """Return True when a cron final response should suppress delivery.

    Recognizes the bracketed ``[SILENT]`` sentinel (whole-response, first line,
    or last line) plus the bracketless ``SILENT`` / ``NO_REPLY`` / ``NO REPLY``
    variants the model emits when it drops the brackets (#51438, #46917).
    Whitespace-trimmed and case-insensitive.  A token buried mid-sentence is
    treated as real content and delivered.
    """
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    if not stripped:
        return False

    def _is_token(line: str) -> bool:
        return " ".join(line.strip().upper().split()) in _CRON_SILENCE_TOKENS

    # Whole response is exactly a token.
    if _is_token(stripped):
        return True
    # Marker on its own first or last line (trailing/leading note on a
    # separate line — e.g. "2 deals filtered\n\n[SILENT]").
    lines = [ln for ln in stripped.splitlines() if ln.strip()]
    if lines and (_is_token(lines[0]) or _is_token(lines[-1])):
        return True
    # Bracketed sentinel used as a same-line prefix — the documented cron
    # pattern "[SILENT] No changes detected".  Restricted to the bracketed
    # form so a bare word like "Silent retry succeeded" is NOT swallowed.
    upper = stripped.upper()
    if upper.startswith("[SILENT]"):
        return True
    return False

# ---------------------------------------------------------------------------
# Persistent thread pool for parallel cron jobs.
# The tick function submits jobs here and returns immediately so the ticker
# thread is never blocked by long-running jobs (e.g. the fixer running 15+ min).
# ---------------------------------------------------------------------------
_parallel_pool: Optional[concurrent.futures.ThreadPoolExecutor] = None
_parallel_pool_max_workers: Optional[int] = None
_running_job_ids: set = set()
_running_lock = threading.Lock()

# Job IDs the gateway shutdown path force-killed the tool subprocess of
# while still in ``_running_job_ids`` (see ``mark_running_jobs_interrupted``
# below). ``run_one_job``'s own completion path checks this set before
# writing its own ``last_status`` so a cron agent thread that keeps running
# in-process after its tool was killed out from under it — and produces a
# plausible-looking final response from truncated output — can never
# overwrite the interrupted status with a false "ok" (#60432).
_interrupted_job_ids: set = set()


def get_running_job_ids() -> "frozenset[str]":
    """Thread-safe snapshot of cron job IDs currently executing.

    A job ID is a member from the moment ``_submit_with_guard`` dispatches
    it onto the parallel/sequential pool until ``_process_job`` returns —
    i.e. for the job's *entire* run, tool calls included, not just the
    ticker's dispatch instant.

    The gateway shutdown path (``gateway/run.py::GatewayRunner.
    _drain_active_agents``) reads this to treat in-flight cron work as
    active the same way it already treats in-flight chat sessions via
    ``_running_agents`` — cron jobs run through their own thread pool here,
    entirely outside that dict, so without this the drain is structurally
    blind to them (#60432).
    """
    with _running_lock:
        return frozenset(_running_job_ids)


def mark_running_jobs_interrupted(reason: str) -> list:
    """Best-effort: mark every currently in-flight cron job interrupted.

    Called by the gateway shutdown path immediately after it force-kills
    tool subprocesses (``process_registry.kill_all()``). A job whose tool
    subprocess was just killed out from under it must never be allowed to
    report success — even though its agent thread is still alive in this
    same process and may go on to produce a plausible-looking final
    response from the now-truncated tool output.

    Records the job IDs in ``_interrupted_job_ids`` BEFORE writing
    ``last_status`` so ``run_one_job``'s own eventual completion for the
    same job (racing in its own thread) sees the flag and skips its normal
    write instead of clobbering this one — see the check near the end of
    ``run_one_job``. This does not attempt to correlate the killed
    subprocess PID to a specific job ID (the process registry tracks PIDs,
    not cron job IDs); any job still dispatched at the moment of a forced
    kill is treated as interrupted, matching the coarser precedent already
    set by ``GatewayRunner._interrupt_running_agents``, which interrupts
    every entry in ``_running_agents`` on a drain timeout without
    per-agent correlation either.

    Returns the list of job IDs marked, for the caller to log.
    """
    with _running_lock:
        job_ids = list(_running_job_ids)
        _interrupted_job_ids.update(job_ids)
    marked = []
    for job_id in job_ids:
        try:
            mark_job_run(job_id, False, reason)
            marked.append(job_id)
        except Exception as e:
            logger.warning("Failed to mark job %s interrupted: %s", job_id, e)
    return marked


def _is_interrupted(job_id: str) -> bool:
    """Non-destructive peek at whether the shutdown path has marked
    ``job_id`` interrupted (see ``mark_running_jobs_interrupted``).

    Called by ``run_one_job`` BEFORE it decides what to deliver — a job
    whose tool subprocess was killed mid-flight may still produce a
    plausible-looking ``final_response`` from the truncated output, and
    that must not go out to the user as if it were a normal result.
    Unlike ``_consume_interrupted_flag`` below, this does not clear the
    flag: the later, authoritative check (right before ``last_status`` is
    written) still needs to see it."""
    with _running_lock:
        return job_id in _interrupted_job_ids


def _consume_interrupted_flag(job_id: str) -> bool:
    """Return True and clear the flag if the shutdown path already marked
    ``job_id`` interrupted (see ``mark_running_jobs_interrupted``).

    Called by ``run_one_job`` right before it would otherwise write its own
    ``last_status``. Consuming (discarding) rather than just checking keeps
    the flag from leaking across a later, unrelated run of the same job ID
    (recurring jobs reuse their ID every fire)."""
    with _running_lock:
        if job_id in _interrupted_job_ids:
            _interrupted_job_ids.discard(job_id)
            return True
        return False


# Sequential (env-mutating) cron jobs — workdir jobs that touch
# process-global runtime state — must run one at a time, but must NOT block the
# ticker thread.  A persistent single-thread executor preserves ordering across
# ticks while keeping dispatch fire-and-forget, the same as the parallel pool.
_sequential_pool: Optional[concurrent.futures.ThreadPoolExecutor] = None


class _ReadWriteLock:
    """Writer-preferring readers-writer lock.

    Guards the process-global ``os.environ["TERMINAL_CWD"]`` override that a
    workdir cron job applies for the whole of its agent run.  Workdir jobs are
    writers: they mutate the shared env and need exclusive access.  Workdir-less
    jobs are readers: they only observe ``TERMINAL_CWD`` (indirectly, via the
    terminal / file / code-exec tools), so any number of them may run
    concurrently with each other, but none may run alongside a writer — that is
    exactly what stops a workdir-less job from picking up another job's workdir
    override and running its commands in the wrong directory.

    Writer preference bounds the wait for a workdir job (dispatched on the
    single-thread sequential pool) so a stream of workdir-less readers cannot
    starve it.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition(threading.Lock())
        self._readers = 0
        self._writer_active = False
        self._writers_waiting = 0

    def acquire_read(self) -> None:
        with self._cond:
            while self._writer_active or self._writers_waiting > 0:
                self._cond.wait()
            self._readers += 1

    def release_read(self) -> None:
        with self._cond:
            self._readers -= 1
            if self._readers == 0:
                self._cond.notify_all()

    def acquire_write(self) -> None:
        with self._cond:
            self._writers_waiting += 1
            try:
                while self._writer_active or self._readers > 0:
                    self._cond.wait()
            finally:
                self._writers_waiting -= 1
            self._writer_active = True

    def release_write(self) -> None:
        with self._cond:
            self._writer_active = False
            self._cond.notify_all()


# Serializes the per-job TERMINAL_CWD override against every other concurrently
# running cron job.  See _ReadWriteLock and run_job for the usage contract.
_terminal_cwd_lock = _ReadWriteLock()


def _get_parallel_pool(max_workers: Optional[int]) -> concurrent.futures.ThreadPoolExecutor:
    """Return (or create) the persistent parallel pool."""
    global _parallel_pool, _parallel_pool_max_workers
    if _parallel_pool is None or _parallel_pool_max_workers != max_workers:
        if _parallel_pool is not None:
            _parallel_pool.shutdown(wait=False, cancel_futures=False)
        _parallel_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="cron-parallel",
        )
        _parallel_pool_max_workers = max_workers
    return _parallel_pool


def _get_sequential_pool() -> concurrent.futures.ThreadPoolExecutor:
    """Return (or create) the persistent single-thread sequential pool.

    A single worker guarantees env-mutating jobs never overlap, even
    across ticks: a job queued by a newer tick waits for the previous tick's
    sequential jobs to finish rather than corrupting their os.environ
    state.
    """
    global _sequential_pool
    if _sequential_pool is None:
        _sequential_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="cron-seq",
        )
    return _sequential_pool


def _shutdown_parallel_pool() -> None:
    """Shut down the persistent pools on process exit."""
    global _parallel_pool, _parallel_pool_max_workers, _sequential_pool
    if _parallel_pool is not None:
        _parallel_pool.shutdown(wait=True, cancel_futures=False)
        _parallel_pool = None
        _parallel_pool_max_workers = None
    if _sequential_pool is not None:
        _sequential_pool.shutdown(wait=True, cancel_futures=False)
        _sequential_pool = None


atexit.register(_shutdown_parallel_pool)

def _terminal_success_reason(job: dict, success: bool, final_response: str) -> Optional[str]:
    """Return an auto-pause reason for explicit terminal-success output."""
    if not success or not job.get("no_agent") or not job.get("disable_on_terminal_success"):
        return None
    text = str(final_response or "").strip()
    if not text:
        return None
    first_line = text.splitlines()[0].strip()
    if first_line.upper().startswith("DONE:"):
        return "terminal success: DONE marker"
    try:
        parsed = json.loads(text.splitlines()[-1])
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if isinstance(parsed, dict) and parsed.get("terminal_success") is True:
        return "terminal success: JSON marker"
    return None

def _interpreter_shutting_down(exc: Optional[BaseException] = None) -> bool:
    """True when the Python interpreter is finalizing.

    A cron tick can fire while the gateway is tearing down — SIGTERM from
    ``hermes update`` / ``hermes gateway stop`` / systemd restart, or an
    OOM-kill. Once finalization starts, ``concurrent.futures`` refuses new
    work with ``RuntimeError: cannot schedule new futures after interpreter
    shutdown`` and asyncio's default executor is gone, so *any* attempt to
    schedule delivery (live-adapter, ``asyncio.run``, or a fresh pool) is
    doomed and only pollutes ``errors.log`` with a traceback. Callers use
    this to skip gracefully with a warning instead of crashing (#58720,
    #55924).

    ``exc`` lets a caller also treat an already-raised scheduling error as a
    shutdown signal: the ``concurrent.futures`` module-global flag can be set
    a hair before ``sys.is_finalizing()`` flips, so matching the error text is
    a safe fallback for that race.
    """
    if sys.is_finalizing():
        return True
    if exc is not None:
        # Match the SHORT prefix deliberately: CPython emits two shutdown
        # variants — "cannot schedule new futures after interpreter shutdown"
        # (asyncio.run_coroutine_threadsafe / a torn-down default executor) and
        # "cannot schedule new futures after shutdown" (a plain
        # ThreadPoolExecutor). Both are documented in #58720. The common prefix
        # catches both; the sibling agent/tool_executor._is_interpreter_shutdown_submit_error
        # matches only the fuller "...after interpreter shutdown" form.
        return "cannot schedule new futures" in str(exc).lower()
    return False


# Backward-compatible module override used by tests and emergency monkeypatches.
_hermes_home: Path | None = None


def _get_hermes_home() -> Path:
    """Resolve Hermes home dynamically while preserving test monkeypatch hooks."""
    return _hermes_home or get_hermes_home()


def _get_lock_paths() -> tuple[Path, Path]:
    """Resolve cron lock paths at call time so profile/env changes are honored."""
    hermes_home = _get_hermes_home()
    lock_dir = hermes_home / "cron"
    return lock_dir, lock_dir / ".tick.lock"


@contextmanager
def _job_profile_context(job_id: str, profile: Optional[str]):
    """Temporarily run a job under a specific Hermes profile.

    Cron jobs are stored and scheduled by the profile running the scheduler, but
    an individual job can opt into a different runtime profile. While active,
    the scheduler's test/override hook and a context-local Hermes home override
    both point at the resolved profile directory so _get_hermes_home(),
    .env/config loading, script resolution, AIAgent construction, and downstream
    get_hermes_home() callers agree on the same home.

    Some existing provider/config paths still load profile .env values through
    os.environ, so profile jobs also snapshot and restore the process
    environment on exit. tick() runs profile jobs sequentially to keep that
    temporary mutation isolated from other scheduled jobs.
    """
    raw_profile = str(profile or "").strip()
    if not raw_profile:
        yield None
        return

    global _hermes_home
    prior_override = _hermes_home
    env_snapshot = os.environ.copy()

    from hermes_cli.profiles import normalize_profile_name, resolve_profile_env
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    normalized_profile = normalize_profile_name(raw_profile)
    try:
        profile_home = Path(resolve_profile_env(normalized_profile)).resolve()
    except (FileNotFoundError, ValueError) as exc:
        logger.warning(
            "Job '%s': configured profile %r no longer valid (%s) — "
            "falling back to scheduler default",
            job_id, raw_profile, exc,
        )
        yield None
        return

    override_token = None
    try:
        override_token = set_hermes_home_override(profile_home)
        _hermes_home = profile_home
        logger.info(
            "Job '%s': using Hermes profile '%s' (%s)",
            job_id,
            normalized_profile,
            profile_home,
        )
        yield normalized_profile
    finally:
        _hermes_home = prior_override
        if override_token is not None:
            reset_hermes_home_override(override_token)
        # Delta-based restore: remove added keys, restore changed keys.
        # Avoids a brief window where other threads see an empty env.
        added = set(os.environ.keys()) - set(env_snapshot.keys())
        for k in added:
            os.environ.pop(k, None)
        for k, v in env_snapshot.items():
            if os.environ.get(k) != v:
                os.environ[k] = v


def _resolve_origin(job: dict) -> Optional[dict]:
    """Extract origin info from a job, preserving any extra routing metadata.

    Treats non-dict origins (free-form provenance strings, ints, lists from
    migration scripts or hand-edited jobs.json) as missing instead of
    crashing with ``AttributeError`` on ``origin.get(...)``. Without this
    guard, a job tagged with e.g. ``"combined-digest-replaces-x-and-y"``
    crashed every fire attempt with
    ``'str' object has no attribute 'get'`` — ``mark_job_run`` recorded the
    failure, but the next tick re-loaded the same poisoned origin and
    crashed identically until the field was patched manually (#18722).
    """
    origin = job.get("origin")
    if not isinstance(origin, dict):
        return None
    platform = origin.get("platform")
    chat_id = origin.get("chat_id")
    if platform and chat_id:
        return origin
    return None


def _cron_mirror_delivery_enabled(job: dict, cfg: Optional[dict] = None) -> bool:
    """Return whether delivery should be attached to its origin session."""
    per_job = job.get("attach_to_session")
    if isinstance(per_job, bool):
        return per_job
    try:
        if cfg is None:
            cfg = load_config() or {}
        return bool((cfg.get("cron", {}) or {}).get("mirror_delivery", False))
    except Exception:
        return False


def _target_matches_origin(
    origin: dict,
    platform_name: str,
    chat_id: str,
    thread_id: Optional[str],
) -> bool:
    """Return true only for the exact conversation that created the job."""
    if not origin:
        return False
    if str(origin.get("platform", "")).lower() != str(platform_name).lower():
        return False
    if str(origin.get("chat_id", "")) != str(chat_id):
        return False
    origin_thread = origin.get("thread_id")
    if origin_thread is not None and str(origin_thread) != str(thread_id or ""):
        return False
    return True


def _maybe_mirror_cron_delivery(
    job: dict,
    platform_name: str,
    chat_id: str,
    mirror_text: str,
    thread_id: Optional[str] = None,
    user_id: Optional[str] = None,
    *,
    enabled: bool = False,
) -> None:
    """Best-effort mirror of a delivery into the originating session."""
    if not enabled:
        return
    text = (mirror_text or "").strip()
    if not text:
        return
    try:
        from gateway.mirror import mirror_to_session

        ok = mirror_to_session(
            platform_name,
            str(chat_id),
            f"[Cron delivery: {job.get('name') or job.get('id', 'cron')}]\n{text}",
            source_label="cron",
            thread_id=thread_id,
            user_id=user_id,
            role="user",
        )
        if ok:
            logger.info(
                "Job '%s': mirrored delivery into %s:%s session transcript",
                job.get("id", "?"),
                platform_name,
                chat_id,
            )
    except Exception as exc:
        logger.debug(
            "Job '%s': delivery mirror failed for %s:%s: %s",
            job.get("id", "?"),
            platform_name,
            chat_id,
            exc,
        )


def _open_continuable_cron_thread(
    job: dict,
    adapter,
    chat_id: str,
    loop,
) -> Optional[str]:
    """Open a dedicated continuation thread when the adapter supports it."""
    create_thread = getattr(adapter, "create_handoff_thread", None)
    if not callable(create_thread) or loop is None:
        return None
    task_name = job.get("name") or job.get("id", "cron")
    try:
        from agent.async_utils import safe_schedule_threadsafe

        future = safe_schedule_threadsafe(
            create_thread(str(chat_id), f"Hermes — {task_name}"),
            loop,
        )
        if future is None:
            return None
        thread_id = future.result(timeout=30)
        return str(thread_id) if thread_id else None
    except Exception as exc:
        logger.debug(
            "Job '%s': continuation thread creation failed on %s: %s",
            job.get("id", "?"),
            getattr(adapter, "name", "?"),
            exc,
        )
        return None


def _seed_cron_thread_session(
    job: dict,
    adapter,
    platform_name: str,
    chat_id: str,
    thread_id: str,
    mirror_text: str,
    chat_name: Optional[str] = None,
) -> None:
    """Create and seed the session matching a newly opened cron thread."""
    text = (mirror_text or "").strip()
    if not text:
        return
    try:
        from gateway.config import Platform
        from gateway.session import SessionSource

        session_store = getattr(adapter, "_session_store", None)
        if session_store is not None:
            session_store.get_or_create_session(
                SessionSource(
                    platform=Platform(platform_name.lower()),
                    chat_id=str(chat_id),
                    chat_name=chat_name,
                    chat_type="thread",
                    user_id="system:cron",
                    user_name="Cron",
                    thread_id=str(thread_id),
                )
            )

        from gateway.mirror import mirror_to_session

        mirror_to_session(
            platform_name,
            str(chat_id),
            f"[Cron delivery: {job.get('name') or job.get('id', 'cron')}]\n{text}",
            source_label="cron",
            thread_id=str(thread_id),
            user_id="system:cron",
            role="user",
        )
    except Exception as exc:
        logger.debug(
            "Job '%s': seeding cron thread session failed for %s:%s:%s: %s",
            job.get("id", "?"),
            platform_name,
            chat_id,
            thread_id,
            exc,
        )


def _seed_cron_channel_session(
    job: dict,
    adapter,
    platform_name: str,
    chat_id: str,
    mirror_text: str,
    *,
    is_dm: bool,
    user_id: Optional[str],
    chat_name: Optional[str] = None,
) -> bool:
    """Create and seed the flat session used by in-channel continuation."""
    text = (mirror_text or "").strip()
    if not text:
        return False
    try:
        from gateway.config import Platform
        from gateway.session import SessionSource

        session_store = getattr(adapter, "_session_store", None)
        if session_store is not None:
            session_store.get_or_create_session(
                SessionSource(
                    platform=Platform(platform_name.lower()),
                    chat_id=str(chat_id),
                    chat_name=chat_name,
                    chat_type="dm" if is_dm else "group",
                    user_id=str(user_id) if user_id else None,
                    thread_id=None,
                )
            )

        from gateway.mirror import mirror_to_session

        return bool(
            mirror_to_session(
                platform_name,
                str(chat_id),
                f"[Cron delivery: {job.get('name') or job.get('id', 'cron')}]\n{text}",
                source_label="cron",
                thread_id=None,
                user_id=str(user_id) if user_id else None,
                role="user",
            )
        )
    except Exception as exc:
        logger.debug(
            "Job '%s': seeding in-channel session failed for %s:%s: %s",
            job.get("id", "?"),
            platform_name,
            chat_id,
            exc,
        )
        return False


def _cron_job_origin_log_suffix(job: dict) -> str:
    """Return safe provenance details for security warnings about a cron job.

    The scheduler normally has no live HTTP request object when it detects a
    bad stored ``context_from`` reference. Including the job's saved origin
    makes future probe logs actionable without exposing secrets: platform/chat
    metadata for gateway-created jobs, and optional source-IP fields for API
    surfaces that persist them in origin metadata.
    """
    origin = job.get("origin")
    if not isinstance(origin, dict):
        return ""

    fields = []
    for key in ("platform", "chat_id", "thread_id", "source_ip", "remote", "forwarded_for"):
        value = origin.get(key)
        if value is None:
            continue
        text = str(value).replace("\r", " ").replace("\n", " ").strip()
        if text:
            fields.append(f"origin_{key}={text[:200]!r}")
    return " " + " ".join(fields) if fields else ""


def _plugin_cron_env_var(platform_name: str) -> str:
    """Return the cron home-channel env var registered by a plugin platform.

    Falls through the platform registry so plugins that set
    ``cron_deliver_env_var`` on their ``PlatformEntry`` get cron delivery
    support without editing this module.
    """
    try:
        from hermes_cli.plugins import discover_plugins
        discover_plugins()  # idempotent
        from gateway.platform_registry import platform_registry
        entry = platform_registry.get(platform_name.lower())
        if entry and entry.cron_deliver_env_var:
            return entry.cron_deliver_env_var
    except Exception:
        pass
    return ""


def _is_known_delivery_platform(platform_name: str) -> bool:
    """Whether ``platform_name`` is a valid cron delivery target.

    Hardcoded built-ins in ``_KNOWN_DELIVERY_PLATFORMS`` are checked first;
    plugin platforms registered via ``PlatformEntry`` are accepted if they
    provide a ``cron_deliver_env_var``.
    """
    name = platform_name.lower()
    if name in _KNOWN_DELIVERY_PLATFORMS:
        return True
    return bool(_plugin_cron_env_var(name))


def _resolve_home_env_var(platform_name: str) -> str:
    """Return the env var name for a platform's cron home channel.

    Built-in platforms are in ``_HOME_TARGET_ENV_VARS``; plugin platforms are
    resolved from the platform registry.
    """
    name = platform_name.lower()
    env_var = _HOME_TARGET_ENV_VARS.get(name)
    if env_var:
        return env_var
    return _plugin_cron_env_var(name)


def _get_home_target_chat_id(platform_name: str) -> str:
    """Return the configured home target chat/room ID for a delivery platform."""
    env_var = _resolve_home_env_var(platform_name)
    if not env_var:
        return ""
    value = os.getenv(env_var, "")
    if not value:
        legacy = _LEGACY_HOME_TARGET_ENV_VARS.get(env_var)
        if legacy:
            value = os.getenv(legacy, "")
    return value


def _get_home_target_thread_id(platform_name: str) -> Optional[str]:
    """Return the optional thread/topic ID for a platform home target.

    Telegram-only override: ``TELEGRAM_CRON_THREAD_ID`` takes precedence over
    ``TELEGRAM_HOME_CHANNEL_THREAD_ID`` for cron delivery. When topic mode is
    enabled, deliveries that land in the root DM (thread_id unset) end up in
    the system-only lobby where the user cannot reply — the gateway returns
    the lobby reminder and drops ``reply_to_message_id`` (#24409). Pointing
    cron at a dedicated topic via this env var lets replies work as expected
    without changing the lobby invariant.
    """
    env_var = _resolve_home_env_var(platform_name)
    if not env_var:
        return None
    if platform_name.lower() == "telegram":
        cron_thread = os.getenv("TELEGRAM_CRON_THREAD_ID", "").strip()
        if cron_thread:
            return cron_thread
    value = os.getenv(f"{env_var}_THREAD_ID", "").strip()
    if not value:
        legacy = _LEGACY_HOME_TARGET_ENV_VARS.get(env_var)
        if legacy:
            value = os.getenv(f"{legacy}_THREAD_ID", "").strip()
    return value or None


def _iter_home_target_platforms():
    """Iterate built-in + plugin platform names that expose a home channel.

    Used by the ``deliver=origin`` fallback when the job has no origin.
    """
    for name in _HOME_TARGET_ENV_VARS:
        yield name
    try:
        from hermes_cli.plugins import discover_plugins
        discover_plugins()  # idempotent
        from gateway.platform_registry import platform_registry
        for entry in platform_registry.plugin_entries():
            if entry.cron_deliver_env_var and entry.name not in _HOME_TARGET_ENV_VARS:
                yield entry.name
    except Exception:
        pass


def cron_delivery_targets() -> list[dict]:
    """Return configured platforms available for unattended cron delivery."""
    try:
        from gateway.config import load_gateway_config

        gateway_config = load_gateway_config()
        connected = {platform.value for platform in gateway_config.get_connected_platforms()}
    except Exception:
        logger.debug("cron_delivery_targets: gateway config unavailable", exc_info=True)
        return []

    targets: list[dict] = []
    for name in _iter_home_target_platforms():
        if name not in connected or not _is_known_delivery_platform(name):
            continue
        env_var = _resolve_home_env_var(name)
        targets.append(
            {
                "id": name,
                "name": name.replace("_", " ").title(),
                "home_target_set": bool(_get_home_target_chat_id(name)),
                "home_env_var": env_var or None,
            }
        )
    return targets


def _resolve_single_delivery_target(job: dict, deliver_value: str) -> Optional[dict]:
    """Resolve one concrete auto-delivery target for a cron job."""

    origin = _resolve_origin(job)

    if deliver_value == "local":
        return None

    if deliver_value == "origin":
        if origin:
            return {
                "platform": origin["platform"],
                "chat_id": str(origin["chat_id"]),
                "thread_id": origin.get("thread_id"),
            }
        # Origin missing (e.g. job created via API/script) — try each
        # platform's home channel as a fallback instead of silently dropping.
        for platform_name in _iter_home_target_platforms():
            chat_id = _get_home_target_chat_id(platform_name)
            if chat_id:
                logger.info(
                    "Job '%s' has deliver=origin but no origin; falling back to %s home channel",
                    job.get("name", job.get("id", "?")),
                    platform_name,
                )
                return {
                    "platform": platform_name,
                    "chat_id": chat_id,
                    "thread_id": _get_home_target_thread_id(platform_name),
                }
        return None

    if ":" in deliver_value:
        platform_name, rest = deliver_value.split(":", 1)
        platform_key = platform_name.lower()

        from tools.send_message_tool import _parse_target_ref

        parsed_chat_id, parsed_thread_id, is_explicit = _parse_target_ref(platform_key, rest)
        if is_explicit:
            chat_id, thread_id = parsed_chat_id, parsed_thread_id
        else:
            chat_id, thread_id = rest, None

        # Resolve human-friendly labels like "Alice (dm)" to real IDs.
        try:
            from gateway.channel_directory import resolve_channel_name
            resolved = resolve_channel_name(platform_key, chat_id)
            if resolved:
                parsed_chat_id, parsed_thread_id, resolved_is_explicit = _parse_target_ref(platform_key, resolved)
                if resolved_is_explicit:
                    chat_id = parsed_chat_id
                    if parsed_thread_id is not None:
                        thread_id = parsed_thread_id
                else:
                    chat_id = resolved
        except Exception:
            pass

        return {
            "platform": platform_name,
            "chat_id": chat_id,
            "thread_id": thread_id,
        }

    platform_name = deliver_value
    if origin and origin.get("platform") == platform_name:
        return {
            "platform": platform_name,
            "chat_id": str(origin["chat_id"]),
            "thread_id": origin.get("thread_id"),
        }

    if not _is_known_delivery_platform(platform_name):
        return None
    chat_id = _get_home_target_chat_id(platform_name)
    if not chat_id:
        return None

    return {
        "platform": platform_name,
        "chat_id": chat_id,
        "thread_id": _get_home_target_thread_id(platform_name),
    }


def _normalize_deliver_value(deliver) -> str:
    """Normalize a stored/submitted ``deliver`` value to its canonical string form.

    The contract is that ``deliver`` is a string (``"local"``, ``"origin"``,
    ``"telegram"``, ``"telegram:-1001:17"``, or comma-separated combinations).
    Historically some callers — MCP clients passing an array, direct edits of
    ``jobs.json``, or stale code paths — have stored a list/tuple like
    ``["telegram"]``.  ``str(["telegram"])`` would serialize to the literal
    string ``"['telegram']"``, which is not a known platform and fails
    resolution silently.  Flatten lists/tuples into a comma-separated string
    so both forms work.  Returns ``"local"`` for anything falsy.
    """
    if deliver is None or deliver == "":
        return "local"
    if isinstance(deliver, (list, tuple)):
        parts = [str(p).strip() for p in deliver if str(p).strip()]
        return ",".join(parts) if parts else "local"
    return str(deliver)


# Routing intent tokens — resolved at fire time, not create time, so a
# job created before Telegram was wired up will pick up Telegram once it
# comes online.  ``all`` expands into the set of connected platforms
# (those with a configured home chat_id) in _expand_routing_tokens.
_ROUTING_TOKENS = frozenset({"all"})


def _expand_routing_tokens(part: str) -> List[str]:
    """Expand a routing-intent token to concrete platform names.

    ``all`` expands to every platform in ``_iter_home_target_platforms()``
    that has a configured home chat_id right now.  Unknown / non-token
    values pass through unchanged as a single-element list, so the caller
    can treat every token uniformly.
    """
    token = part.lower()
    if token not in _ROUTING_TOKENS:
        return [part]
    expanded: List[str] = []
    for platform_name in _iter_home_target_platforms():
        if _get_home_target_chat_id(platform_name):
            expanded.append(platform_name)
    return expanded


def _resolve_delivery_targets(job: dict) -> List[dict]:
    """Resolve all concrete auto-delivery targets for a cron job.

    Accepts the legacy comma-separated ``deliver`` string plus the
    ``all`` routing-intent token, which expands to every platform with
    a configured home channel.  Tokens may be combined with explicit
    targets: ``origin,all`` and ``all,telegram:-100:17`` both work.
    Duplicate (platform, chat_id, thread_id) tuples are collapsed by the
    existing dedup pass.
    """
    deliver = _normalize_deliver_value(job.get("deliver", "local"))
    if deliver == "local":
        return []

    raw_parts = [p.strip() for p in deliver.split(",") if p.strip()]

    # Expand routing intents.
    parts: List[str] = []
    for raw in raw_parts:
        parts.extend(_expand_routing_tokens(raw))

    seen = set()
    targets = []
    for part in parts:
        target = _resolve_single_delivery_target(job, part)
        if target:
            key = (target["platform"].lower(), str(target["chat_id"]), target.get("thread_id"))
            if key not in seen:
                seen.add(key)
                targets.append(target)
    return targets


def _resolve_delivery_target(job: dict) -> Optional[dict]:
    """Resolve the concrete auto-delivery target for a cron job, if any."""
    targets = _resolve_delivery_targets(job)
    return targets[0] if targets else None


def _target_is_explicit_top_level(job: dict, target: dict) -> bool:
    """True when the stored deliver string intentionally names a channel only."""
    if target.get("thread_id"):
        return False
    target_platform = str(target.get("platform") or "").strip().lower()
    target_chat_id = str(target.get("chat_id") or "").strip()
    if not target_platform or not target_chat_id:
        return False

    deliver = _normalize_deliver_value(job.get("deliver", "local"))
    for raw in [p.strip() for p in deliver.split(",") if p.strip()]:
        for part in _expand_routing_tokens(raw):
            if ":" not in part:
                continue
            platform_name, rest = part.split(":", 1)
            platform_key = platform_name.lower()
            if platform_key != target_platform:
                continue

            from tools.send_message_tool import _parse_target_ref

            parsed_chat_id, parsed_thread_id, is_explicit = _parse_target_ref(platform_key, rest)
            if is_explicit:
                if parsed_thread_id is None and str(parsed_chat_id) == target_chat_id:
                    return True
                continue
            try:
                from gateway.channel_directory import resolve_channel_name

                resolved = resolve_channel_name(platform_key, rest)
            except Exception:
                resolved = None
            if not resolved:
                continue
            parsed_chat_id, parsed_thread_id, resolved_is_explicit = _parse_target_ref(platform_key, resolved)
            if resolved_is_explicit:
                if parsed_thread_id is None and str(parsed_chat_id) == target_chat_id:
                    return True
            elif str(resolved).strip() == target_chat_id:
                return True
    return False


# Media extension sets — audio routing is centralized in gateway.platforms.base
# via should_send_media_as_audio() so Telegram-specific rules stay in one place.
_VIDEO_EXTS = frozenset({'.mp4', '.mov', '.avi', '.mkv', '.webm', '.3gp'})
_IMAGE_EXTS = frozenset({'.jpg', '.jpeg', '.png', '.webp', '.gif'})


def _send_media_via_adapter(
    adapter,
    chat_id: str,
    media_files: list,
    metadata: dict | None,
    loop,
    job: dict,
    platform=None,
) -> None:
    """Send extracted MEDIA files as native platform attachments via a live adapter.

    Routes each file to the appropriate adapter method (send_voice, send_image_file,
    send_video, send_document) based on file extension — mirroring the routing logic
    in ``BasePlatformAdapter._process_message_background``.
    """
    from pathlib import Path

    from gateway.platforms.base import BasePlatformAdapter, should_send_media_as_audio

    media_files = BasePlatformAdapter.filter_media_delivery_paths(media_files)

    for media_path, _is_voice in media_files:
        try:
            ext = Path(media_path).suffix.lower()
            route_platform = platform if platform is not None else getattr(adapter, "platform", None)
            if should_send_media_as_audio(route_platform, ext, is_voice=_is_voice):
                coro = adapter.send_voice(chat_id=chat_id, audio_path=media_path, metadata=metadata)
            elif ext in _VIDEO_EXTS:
                coro = adapter.send_video(chat_id=chat_id, video_path=media_path, metadata=metadata)
            elif ext in _IMAGE_EXTS:
                coro = adapter.send_image_file(chat_id=chat_id, image_path=media_path, metadata=metadata)
            else:
                coro = adapter.send_document(chat_id=chat_id, file_path=media_path, metadata=metadata)

            from agent.async_utils import safe_schedule_threadsafe
            future = safe_schedule_threadsafe(coro, loop)
            if future is None:
                logger.warning(
                    "Job '%s': cannot send media %s, gateway loop unavailable",
                    job.get("id", "?"), media_path,
                )
                return
            try:
                result = future.result(timeout=30)
            except TimeoutError:
                future.cancel()
                raise
            if result and not getattr(result, "success", True):
                logger.warning(
                    "Job '%s': media send failed for %s: %s",
                    job.get("id", "?"), media_path, getattr(result, "error", "unknown"),
                )
        except Exception as e:
            logger.warning("Job '%s': failed to send media %s: %s", job.get("id", "?"), media_path, e)


async def _send_discord_cron_feature_summaries(
    adapter,
    *,
    chat_id: str,
    thread_id: Optional[str],
    job: dict,
    content: str,
    pr_urls: list[str],
) -> int:
    """Create standard non-goal Discord feature-summary embeds for cron PRs."""
    if not pr_urls:
        return 0
    resolve_channel = getattr(adapter, "_resolve_channel_by_id", None)
    initialize = getattr(adapter, "initialize_feature_summary", None)
    update = getattr(adapter, "update_feature_summary", None)
    if not callable(resolve_channel) or not callable(initialize) or not callable(update):
        return 0

    summary_channel_id = str(thread_id or chat_id or "").strip()
    if not summary_channel_id:
        return 0
    channel = await resolve_channel(summary_channel_id)
    if channel is None:
        return 0

    parent = None
    if thread_id:
        parent_id = str(chat_id or "").strip()
        if parent_id and parent_id != summary_channel_id:
            parent = await resolve_channel(parent_id)
        if parent is None:
            parent = getattr(channel, "parent", None)

    created = 0
    for pr_url in pr_urls:
        project_context = _cron_discord_feature_project_context(job, pr_url)
        handle = await initialize(
            channel,
            parent_channel=parent,
            initial_request=_cron_discord_feature_initial_request(job, pr_url),
            project_context=project_context or None,
        )
        if not handle:
            continue
        handle["pr_url"] = pr_url
        if project_context and not handle.get("project_context"):
            handle["project_context"] = project_context
        ok = await update(
            handle,
            final_response=_cron_discord_feature_outcome(content, pr_url),
            status="Complete",
            title=_cron_discord_feature_title(job, pr_url),
        )
        if ok is False:
            logger.warning(
                "Job '%s': Discord cron feature-summary update failed for %s",
                job.get("id", "?"),
                pr_url,
            )
            continue
        created += 1
    return created


def _deliver_result(
    job: dict,
    content: str,
    adapters=None,
    loop=None,
    *,
    cron_feature_summaries: bool = True,
) -> Optional[str]:
    """
    Deliver job output to the configured target(s) (origin chat, specific platform, etc.).

    When ``adapters`` and ``loop`` are provided (gateway is running), tries to
    use the live adapter first — this supports E2EE rooms (e.g. Matrix) where
    the standalone HTTP path cannot encrypt.  Falls back to standalone send if
    the adapter path fails or is unavailable.

    Returns None on success, or an error string on failure.
    """
    targets = _resolve_delivery_targets(job)
    if not targets:
        deliver_value = _normalize_deliver_value(job.get("deliver", "local"))
        if deliver_value in {"local", "origin"}:
            return None
        if deliver_value != "local":
            msg = f"no delivery target resolved for deliver={deliver_value}"
            logger.warning("Job '%s': %s", job["id"], msg)
            return msg
        return None

    from tools.send_message_tool import _send_to_platform
    from gateway.config import load_gateway_config, Platform

    # Optionally wrap the content with a header/footer so the user knows this
    # is a cron delivery.  Wrapping is on by default; set cron.wrap_response: false
    # in config.yaml for clean output.
    wrap_response = True
    user_cfg = None
    try:
        user_cfg = load_config()
        wrap_response = user_cfg.get("cron", {}).get("wrap_response", True)
    except Exception:
        pass

    if wrap_response:
        task_name = job.get("name", job["id"])
        job_id = job.get("id", "")
        delivery_content = (
            f"Cronjob Response: {task_name}\n"
            f"(job_id: {job_id})\n"
            f"-------------\n\n"
            f"{content}\n\n"
            f"To stop or manage this job, send me a new message (e.g. \"stop reminder {task_name}\")."
        )
    else:
        delivery_content = content

    # Extract MEDIA: tags so attachments are forwarded as files, not raw text
    from gateway.platforms.base import BasePlatformAdapter
    media_files, cleaned_delivery_content = BasePlatformAdapter.extract_media(delivery_content)
    media_files = BasePlatformAdapter.filter_media_delivery_paths(media_files)

    mirror_enabled = _cron_mirror_delivery_enabled(job, user_cfg)
    mirror_text = ""
    if mirror_enabled:
        _, mirror_text = BasePlatformAdapter.extract_media(content)
        mirror_text = (mirror_text or "").strip()

    try:
        config = load_gateway_config()
    except Exception as e:
        msg = f"failed to load gateway config: {e}"
        logger.error("Job '%s': %s", job["id"], msg)
        return msg

    delivery_errors = []

    for target in targets:
        target_errors = []
        platform_name = target["platform"]
        chat_id = target["chat_id"]
        thread_id = target.get("thread_id")

        # Diagnostic: log thread_id for topic-aware delivery debugging
        origin = _resolve_origin(job) or {}
        origin_thread = origin.get("thread_id")
        if origin_thread and not thread_id and not _target_is_explicit_top_level(job, target):
            logger.warning(
                "Job '%s': origin has thread_id=%s but delivery target lost it "
                "(deliver=%s, target=%s)",
                job["id"], origin_thread, job.get("deliver", "local"), target,
            )
        elif thread_id:
            logger.debug(
                "Job '%s': delivering to %s:%s thread_id=%s",
                job["id"], platform_name, chat_id, thread_id,
            )

        mirror_this_target = mirror_enabled and _target_matches_origin(
            origin,
            platform_name,
            chat_id,
            thread_id,
        )
        origin_user_id = origin.get("user_id") if mirror_this_target else None

        # Built-in names resolve to their enum member; plugin platform names
        # create dynamic members via Platform._missing_().
        try:
            platform = Platform(platform_name.lower())
        except (ValueError, KeyError):
            msg = f"unknown platform '{platform_name}'"
            logger.warning("Job '%s': %s", job["id"], msg)
            delivery_errors.append(msg)
            continue

        pconfig = config.platforms.get(platform)
        if not pconfig or not pconfig.enabled:
            msg = f"platform '{platform_name}' not configured/enabled"
            logger.warning("Job '%s': %s", job["id"], msg)
            delivery_errors.append(msg)
            continue

        # Prefer the live adapter when the gateway is running — this supports E2EE
        # rooms (e.g. Matrix) where the standalone HTTP path cannot encrypt.
        runtime_adapter = (adapters or {}).get(platform)
        delivered = False
        surface_mode = "thread"
        try:
            raw_surface = (pconfig.extra or {}).get("cron_continuable_surface")
            if str(raw_surface or "").strip().lower() == "in_channel":
                surface_mode = "in_channel"
        except Exception:
            pass
        in_channel_surface = surface_mode == "in_channel"
        if in_channel_surface and runtime_adapter is not None and not getattr(
            runtime_adapter,
            "supports_inchannel_continuable",
            False,
        ):
            in_channel_surface = False

        origin_chat_type = str(origin.get("chat_type") or "").lower()
        is_dm_target = origin_chat_type == "dm" or (
            not origin_chat_type and str(chat_id).startswith("D")
        )
        thread_seeded = False
        inchannel_seeded = False
        opened_thread_id: Optional[str] = None
        if (
            mirror_this_target
            and not in_channel_surface
            and runtime_adapter is not None
            and loop is not None
            and not thread_id
        ):
            opened_thread_id = _open_continuable_cron_thread(
                job,
                runtime_adapter,
                chat_id,
                loop,
            )
            if opened_thread_id:
                thread_id = opened_thread_id

        if runtime_adapter is not None and loop is not None and getattr(loop, "is_running", lambda: False)():
            send_metadata = {"thread_id": thread_id} if thread_id else None
            cron_pr_urls = _extract_github_pr_urls(content) if cron_feature_summaries else []
            try:
                # For Discord cron PR reports, create the simple action-request
                # / feature-summary embed first, then send the detailed text body.
                # This keeps the channel visually anchored on the rich summary and
                # makes the follow-up read like the post-embed evidence block.
                if platform == Platform.DISCORD and cron_pr_urls:
                    from agent.async_utils import safe_schedule_threadsafe

                    future = safe_schedule_threadsafe(
                        _send_discord_cron_feature_summaries(
                            runtime_adapter,
                            chat_id=str(chat_id),
                            thread_id=str(thread_id) if thread_id else None,
                            job=job,
                            content=content,
                            pr_urls=cron_pr_urls,
                        ),
                        loop,
                    )
                    if future is None:
                        msg = "Discord cron feature-summary embed scheduling failed"
                        logger.warning("Job '%s': %s", job["id"], msg)
                        delivery_errors.append(msg)
                    else:
                        try:
                            created = future.result(timeout=30)
                        except TimeoutError:
                            future.cancel()
                            raise
                        if created < len(cron_pr_urls):
                            msg = (
                                "Discord cron feature-summary embeds created "
                                f"{created}/{len(cron_pr_urls)}"
                            )
                            logger.warning("Job '%s': %s", job["id"], msg)
                            delivery_errors.append(msg)

                # Send cleaned text (MEDIA tags stripped) — not the raw content
                text_to_send = cleaned_delivery_content.strip()
                adapter_ok = True
                if text_to_send:
                    from agent.async_utils import safe_schedule_threadsafe
                    future = safe_schedule_threadsafe(
                        runtime_adapter.send(chat_id, text_to_send, metadata=send_metadata),
                        loop,
                    )
                    if future is None:
                        adapter_ok = False
                    else:
                        try:
                            send_result = future.result(timeout=60)
                        except TimeoutError:
                            future.cancel()
                            raise
                        if send_result and not getattr(send_result, "success", True):
                            err = getattr(send_result, "error", "unknown")
                            logger.warning(
                                "Job '%s': live adapter send to %s:%s failed (%s), falling back to standalone",
                                job["id"], platform_name, chat_id, err,
                            )
                            adapter_ok = False  # fall through to standalone path
                        elif (
                            send_result
                            and thread_id
                            and getattr(send_result, "raw_response", None)
                            and send_result.raw_response.get("thread_fallback")
                        ):
                            requested_thread_id = send_result.raw_response.get("requested_thread_id") or thread_id
                            msg = (
                                f"configured thread_id {requested_thread_id} for "
                                f"{platform_name}:{chat_id} was not found; delivered without thread_id"
                            )
                            logger.warning("Job '%s': %s", job["id"], msg)
                            delivery_errors.append(msg)

                # Send extracted media files as native attachments via the live adapter
                if adapter_ok and media_files:
                    _send_media_via_adapter(
                        runtime_adapter,
                        chat_id,
                        media_files,
                        send_metadata,
                        loop,
                        job,
                        platform=platform,
                    )

                if adapter_ok:
                    logger.info("Job '%s': delivered to %s:%s via live adapter", job["id"], platform_name, chat_id)
                    delivered = True
                    if opened_thread_id:
                        _seed_cron_thread_session(
                            job,
                            runtime_adapter,
                            platform_name,
                            chat_id,
                            opened_thread_id,
                            mirror_text,
                            chat_name=origin.get("chat_name"),
                        )
                        thread_seeded = True
                    elif in_channel_surface and mirror_this_target:
                        inchannel_seeded = _seed_cron_channel_session(
                            job,
                            runtime_adapter,
                            platform_name,
                            chat_id,
                            mirror_text,
                            is_dm=is_dm_target,
                            user_id=origin_user_id,
                            chat_name=origin.get("chat_name"),
                        )
                    _maybe_mirror_cron_delivery(
                        job,
                        platform_name,
                        chat_id,
                        mirror_text,
                        thread_id=thread_id,
                        user_id=origin_user_id,
                        enabled=(
                            mirror_this_target
                            and not thread_seeded
                            and not inchannel_seeded
                        ),
                    )
            except Exception as e:
                logger.warning(
                    "Job '%s': live adapter delivery to %s:%s failed (%s), falling back to standalone",
                    job["id"], platform_name, chat_id, e,
                )

        if not delivered:
            # If the interpreter is finalizing (gateway SIGTERM / restart /
            # OOM), scheduling any new delivery is futile — asyncio.run and a
            # fresh ThreadPoolExecutor both raise "cannot schedule new futures
            # after interpreter shutdown". Skip gracefully with a warning
            # rather than emitting an ERROR traceback on every restart-race
            # (#58720, #55924).
            if _interpreter_shutting_down():
                msg = f"delivery to {platform_name}:{chat_id} skipped — interpreter is shutting down"
                logger.warning("Job '%s': %s", job["id"], msg)
                target_errors.append(msg)
                delivery_errors.extend(target_errors)
                continue
            # Standalone path: run the async send in a fresh event loop (safe from any thread)
            coro = _send_to_platform(platform, pconfig, chat_id, cleaned_delivery_content, thread_id=thread_id, media_files=media_files)
            try:
                result = asyncio.run(coro)
            except RuntimeError as run_err:
                # asyncio.run() checks for a running loop before awaiting the coroutine;
                # when it raises, the original coro was never started — close it to
                # prevent "coroutine was never awaited" RuntimeWarning, then retry in a
                # fresh thread that has no running loop.
                coro.close()
                # If the RuntimeError is the interpreter-finalization signal,
                # the fresh-thread fallback would fail identically — skip
                # gracefully instead of logging a shutdown-race traceback.
                if _interpreter_shutting_down(run_err):
                    msg = f"delivery to {platform_name}:{chat_id} skipped — interpreter is shutting down"
                    logger.warning("Job '%s': %s", job["id"], msg)
                    target_errors.append(msg)
                    delivery_errors.extend(target_errors)
                    continue
                # The thread-pool fallback can itself raise (SMTP ConnectionError,
                # future.result timeout, etc.). An exception raised inside this
                # `except RuntimeError` block is NOT caught by the sibling
                # `except Exception` below — it would escape _deliver_result()
                # and crash the whole delivery loop, silently skipping every
                # remaining target (#47163). Wrap the fallback in its own
                # try/except so a per-target failure is logged and the loop
                # continues to the next target.
                try:
                    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                    try:
                        future = pool.submit(asyncio.run, _send_to_platform(platform, pconfig, chat_id, cleaned_delivery_content, thread_id=thread_id, media_files=media_files))
                        result = future.result(timeout=30)
                    finally:
                        pool.shutdown(wait=False)
                except Exception as e:
                    # A shutdown-race here is expected during teardown; downgrade
                    # to a warning so it doesn't read as a genuine failure.
                    if _interpreter_shutting_down(e):
                        msg = f"delivery to {platform_name}:{chat_id} skipped — interpreter is shutting down"
                        logger.warning("Job '%s': %s", job["id"], msg)
                        target_errors.append(msg)
                        delivery_errors.extend(target_errors)
                        continue
                    msg = f"delivery to {platform_name}:{chat_id} failed: {e}"
                    logger.error("Job '%s': %s", job["id"], msg, exc_info=True)
                    target_errors.extend([msg])
                    delivery_errors.extend(target_errors)
                    continue
            except Exception as e:
                msg = f"delivery to {platform_name}:{chat_id} failed: {e}"
                logger.error("Job '%s': %s", job["id"], msg)
                delivery_errors.append(msg)
                continue

            if result and result.get("error"):
                msg = f"delivery error: {result['error']}"
                logger.error("Job '%s': %s", job["id"], msg)
                delivery_errors.append(msg)
                continue

            logger.info("Job '%s': delivered to %s:%s", job["id"], platform_name, chat_id)
            if opened_thread_id and runtime_adapter is not None:
                _seed_cron_thread_session(
                    job,
                    runtime_adapter,
                    platform_name,
                    chat_id,
                    opened_thread_id,
                    mirror_text,
                    chat_name=origin.get("chat_name"),
                )
                thread_seeded = True
            elif in_channel_surface and mirror_this_target and runtime_adapter is not None:
                inchannel_seeded = _seed_cron_channel_session(
                    job,
                    runtime_adapter,
                    platform_name,
                    chat_id,
                    mirror_text,
                    is_dm=is_dm_target,
                    user_id=origin_user_id,
                    chat_name=origin.get("chat_name"),
                )
            _maybe_mirror_cron_delivery(
                job,
                platform_name,
                chat_id,
                mirror_text,
                thread_id=thread_id,
                user_id=origin_user_id,
                enabled=(
                    mirror_this_target
                    and not thread_seeded
                    and not inchannel_seeded
                ),
            )

    if delivery_errors:
        return "; ".join(delivery_errors)
    return None


_DEFAULT_SCRIPT_TIMEOUT = 120  # seconds
# Backward-compatible module override used by tests and emergency monkeypatches.
_SCRIPT_TIMEOUT = _DEFAULT_SCRIPT_TIMEOUT
_SCRIPT_STREAM_HEAD_BYTES = 8 * 1024
_SCRIPT_STREAM_TAIL_BYTES = 8 * 1024
_CRON_CONTEXT_INJECTION_BUDGET = 12_000
_CRON_SKILL_INJECTION_BUDGET = 12_000
_CRON_IDENTIFIER_PREVIEW_CHARS = 1_200
_RUN_CLAIM_HEARTBEAT_SECONDS = 60.0


def _get_script_timeout() -> int:
    """Resolve cron pre-run script timeout from module/env/config with a safe default."""
    if _SCRIPT_TIMEOUT != _DEFAULT_SCRIPT_TIMEOUT:
        try:
            timeout = int(float(_SCRIPT_TIMEOUT))
            if timeout > 0:
                return timeout
        except Exception:
            logger.warning("Invalid patched _SCRIPT_TIMEOUT=%r; using env/config/default", _SCRIPT_TIMEOUT)

    env_value = os.getenv("HERMES_CRON_SCRIPT_TIMEOUT", "").strip()
    if env_value:
        try:
            timeout = int(float(env_value))
            if timeout > 0:
                return timeout
        except Exception:
            logger.warning("Invalid HERMES_CRON_SCRIPT_TIMEOUT=%r; using config/default", env_value)

    try:
        cfg = load_config() or {}
        cron_cfg = cfg.get("cron", {}) if isinstance(cfg, dict) else {}
        configured = cron_cfg.get("script_timeout_seconds")
        if configured is not None:
            timeout = int(float(configured))
            if timeout > 0:
                return timeout
    except Exception as exc:
        logger.debug("Failed to load cron script timeout from config: %s", exc)

    return _DEFAULT_SCRIPT_TIMEOUT


class _BoundedByteCapture:
    """Keep first N and last N bytes while counting total observed bytes."""

    def __init__(
        self,
        *,
        head_limit: int = _SCRIPT_STREAM_HEAD_BYTES,
        tail_limit: int = _SCRIPT_STREAM_TAIL_BYTES,
    ) -> None:
        self.head_limit = max(0, int(head_limit))
        self.tail_limit = max(0, int(tail_limit))
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0
        self._lock = threading.Lock()

    def append(self, chunk: bytes) -> None:
        if not chunk:
            return
        if not isinstance(chunk, (bytes, bytearray)):
            chunk = bytes(chunk)
        with self._lock:
            self.total += len(chunk)
            if len(self.head) < self.head_limit:
                take = min(self.head_limit - len(self.head), len(chunk))
                if take:
                    self.head.extend(chunk[:take])
            if self.tail_limit:
                self.tail.extend(chunk)
                if len(self.tail) > self.tail_limit:
                    del self.tail[: len(self.tail) - self.tail_limit]

    def render(self, label: str) -> str:
        with self._lock:
            total = self.total
            head = bytes(self.head)
            tail = bytes(self.tail)

        if total <= len(head):
            data = head
        else:
            tail_start = max(0, total - len(tail))
            head_end = len(head)
            if tail_start <= head_end:
                overlap = head_end - tail_start
                data = head + tail[overlap:]
            else:
                omitted = max(0, tail_start - head_end)
                marker = (
                    f"\n\n[... {label} truncated: omitted {omitted} bytes; "
                    f"observed {total} bytes; kept first {len(head)} and last {len(tail)} bytes ...]\n\n"
                ).encode("utf-8")
                data = head + marker + tail
        return data.decode("utf-8", errors="replace").strip()


def _read_stream_bounded(stream: Any, capture: _BoundedByteCapture) -> None:
    try:
        while True:
            chunk = stream.read(8192)
            if not chunk:
                break
            capture.append(chunk)
    except Exception:
        logger.debug("Cron script stream reader failed", exc_info=True)
    finally:
        try:
            stream.close()
        except Exception:
            pass


def _redact_script_stream(text: str) -> str:
    try:
        from agent.redact import redact_sensitive_text

        return redact_sensitive_text(text)
    except Exception:
        return text


def _head_tail_char_preview(text: str, max_chars: int, *, label: str) -> str:
    text = str(text or "")
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text

    marker = ""
    available = max_chars
    omitted = len(text)
    for _ in range(4):
        marker = (
            f"\n\n[… {label} truncated: omitted {omitted} of {len(text)} chars; "
            "kept head and tail.]\n\n"
        )
        available = max_chars - len(marker)
        if available <= 0:
            break
        new_omitted = len(text) - available
        if new_omitted == omitted:
            break
        omitted = new_omitted

    if available <= 0:
        return text[:max_chars]

    head_chars = max(0, int(available * 0.70))
    tail_chars = max(0, available - head_chars)
    return text[:head_chars] + marker + (text[-tail_chars:] if tail_chars else "")


class _AggregateTextBudget:
    def __init__(self, max_chars: int, *, label_prefix: str) -> None:
        self.remaining = max(0, int(max_chars))
        self.label_prefix = label_prefix

    def fit(self, text: str, label: str) -> str:
        if self.remaining <= 0:
            return ""
        fitted = _head_tail_char_preview(
            str(text or ""),
            self.remaining,
            label=f"{self.label_prefix} {label}".strip(),
        )
        self.remaining = max(0, self.remaining - len(fitted))
        return fitted


def _format_identifier_preview(identifiers: list[str], *, max_chars: int = _CRON_IDENTIFIER_PREVIEW_CHARS) -> str:
    cleaned = [str(item).strip() for item in identifiers if str(item).strip()]
    if not cleaned:
        return "(none)"
    full = ", ".join(cleaned)
    if len(full) <= max_chars:
        return full

    def _candidate(head_count: int, tail_count: int) -> str:
        head = cleaned[:head_count]
        tail = cleaned[len(cleaned) - tail_count:] if tail_count else []
        omitted = len(cleaned) - len(head) - len(tail)
        return ", ".join([*head, f"… [{omitted} identifier(s) omitted] …", *tail])

    best: tuple[int, int, str] | None = None
    for head_count in range(1, len(cleaned)):
        for tail_count in range(1, len(cleaned) - head_count + 1):
            if head_count + tail_count >= len(cleaned):
                continue
            candidate = _candidate(head_count, tail_count)
            if len(candidate) <= max_chars and (
                best is None or head_count + tail_count > best[0] + best[1]
            ):
                best = (head_count, tail_count, candidate)
    if best is None:
        omitted = max(0, len(cleaned) - 1)
        return f"{cleaned[0][:max(24, max_chars // 3)]} … [{omitted} identifier(s) omitted]"
    return best[2]
def _read_windows_pyvenv_cfg(venv_dir: Path) -> dict[str, str]:
    cfg_path = venv_dir / "pyvenv.cfg"
    try:
        lines = cfg_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}

    parsed: dict[str, str] = {}
    for raw in lines:
        if "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        parsed[key.strip().lower()] = value.strip()
    return parsed


def _windows_cron_python_invocation(python_exe: str) -> tuple[str, dict[str, str]]:
    """Return an output-capable hidden Python invocation for Windows scripts.

    Cron scripts capture stdout/stderr, so using ``pythonw.exe`` directly can
    lose script output.  uv-created venv ``python.exe`` launchers are also a
    problem: even with CREATE_NO_WINDOW, the launcher can re-exec the base
    console interpreter and flash a visible window.  For uv venvs, bypass the
    launcher and run the base ``python.exe`` directly with the venv paths
    overlaid in the environment.
    """
    if sys.platform != "win32":
        return python_exe, {}

    interpreter = Path(python_exe)
    venv_dir = interpreter.parent.parent
    env_overlay: dict[str, str] = {}

    if interpreter.name.lower() == "pythonw.exe":
        sibling = interpreter.with_name("python.exe")
        if sibling.exists():
            interpreter = sibling

    cfg = _read_windows_pyvenv_cfg(venv_dir)
    home = cfg.get("home", "")
    site_packages = venv_dir / "Lib" / "site-packages"
    if "uv" in cfg and home:
        base_python = Path(home) / "python.exe"
        if base_python.exists() and site_packages.exists():
            interpreter = base_python
            env_overlay["VIRTUAL_ENV"] = str(venv_dir)
            pythonpath_entries = [
                str(Path(__file__).resolve().parents[1]),
                str(site_packages),
            ]
            existing_pythonpath = os.environ.get("PYTHONPATH", "")
            if existing_pythonpath:
                pythonpath_entries.append(existing_pythonpath)
            env_overlay["PYTHONPATH"] = os.pathsep.join(pythonpath_entries)

    return str(interpreter), env_overlay


def _run_job_script(script_path: str) -> tuple[bool, str]:
    """Execute a cron job's data-collection script and capture its output.

    Scripts must reside within HERMES_HOME/scripts/.  Both relative and
    absolute paths are resolved and validated against this directory to
    prevent arbitrary script execution via path traversal or absolute
    path injection.

    Supported interpreters (chosen by file extension):

    * ``.sh`` / ``.bash`` — run with ``/bin/bash``
    * anything else — run with the current Python interpreter
      (``sys.executable``), preserving the original behaviour for
      Python-based pre-check and data-collection scripts.

    Shell support lets ``no_agent=True`` jobs ship classic bash watchdogs
    (the `memory-watchdog.sh` pattern) without wrapping them in Python.

    Args:
        script_path: Path to the script.  Relative paths are resolved
            against HERMES_HOME/scripts/.  Absolute and ~-prefixed paths
            are also validated to ensure they stay within the scripts dir.

    Returns:
        (success, output) — on failure *output* contains the error message so the
        LLM can report the problem to the user.
    """
    scripts_dir = _get_hermes_home() / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    scripts_dir_resolved = scripts_dir.resolve()

    if "\x00" in str(script_path):
        return False, f"Blocked: script path contains a NUL byte: {script_path!r}"

    raw = Path(script_path).expanduser()
    if raw.is_absolute():
        path = raw.resolve()
    else:
        path = (scripts_dir / raw).resolve()

    # Guard against path traversal, absolute path injection, and symlink
    # escape — scripts MUST reside within HERMES_HOME/scripts/.
    try:
        path.relative_to(scripts_dir_resolved)
    except ValueError:
        return False, (
            f"Blocked: script path resolves outside the scripts directory "
            f"({scripts_dir_resolved}): {script_path!r}"
        )

    if not path.exists():
        return False, f"Script not found: {path}"
    if not path.is_file():
        return False, f"Script path is not a file: {path}"

    script_timeout = _get_script_timeout()

    # Pick an interpreter by extension.  Bash for .sh/.bash, Python for
    # everything else.  We deliberately do NOT honour the file's own
    # shebang: the scripts dir is trusted, but keeping the interpreter
    # choice explicit here keeps the allowed surface small and auditable.
    suffix = path.suffix.lower()
    if suffix in {".sh", ".bash"}:
        # Resolve bash dynamically so Windows (Git Bash) and Linux/macOS
        # all work.  On native Windows without Git for Windows installed
        # shutil.which returns None — fall back to a clear error rather
        # than a FileNotFoundError with a confusing "[WinError 2]"
        # traceback.
        _bash = shutil.which("bash") or (
            "/bin/bash" if os.path.isfile("/bin/bash") else None
        )
        if _bash is None:
            return False, (
                f"Cannot run .sh/.bash script {path.name!r}: bash not found on PATH. "
                "On Windows, install Git for Windows (which ships Git Bash) "
                "or rewrite the script as Python (.py)."
        )
        argv = [_bash, str(path)]
        env_overlay: dict[str, str] = {}
    else:
        python_exe, env_overlay = _windows_cron_python_invocation(sys.executable)
        argv = [python_exe, str(path)]

    try:
        from tools.environments.local import _sanitize_subprocess_env

        run_env = _sanitize_subprocess_env(os.environ.copy())
    except Exception:
        run_env = os.environ.copy()
    run_env.update(env_overlay)
    run_env["HERMES_HOME"] = str(_get_hermes_home())
    try:
        from hermes_constants import get_subprocess_home

        profile_home = get_subprocess_home()
        if profile_home:
            run_env["HOME"] = profile_home
    except Exception:
        pass

    try:
        popen_kwargs = {"creationflags": windows_hide_flags()} if sys.platform == "win32" else {}
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            cwd=str(path.parent),
            env=run_env,
            **popen_kwargs,
        )
        stdout_capture = _BoundedByteCapture()
        stderr_capture = _BoundedByteCapture()
        stdout_thread = threading.Thread(
            target=_read_stream_bounded,
            args=(proc.stdout, stdout_capture),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_read_stream_bounded,
            args=(proc.stderr, stderr_capture),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()

        try:
            returncode = proc.wait(timeout=script_timeout)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
            stdout_thread.join(timeout=2)
            stderr_thread.join(timeout=2)
            return False, f"Script timed out after {script_timeout}s: {path}"

        stdout_thread.join(timeout=2)
        stderr_thread.join(timeout=2)

        stdout = _redact_script_stream(stdout_capture.render("stdout"))
        stderr = _redact_script_stream(stderr_capture.render("stderr"))

        if returncode != 0:
            parts = [f"Script exited with code {returncode}"]
            if stderr:
                parts.append(f"stderr:\n{stderr}")
            if stdout:
                parts.append(f"stdout:\n{stdout}")
            return False, "\n".join(parts)

        return True, stdout

    except Exception as exc:
        return False, f"Script execution failed: {exc}"


def _run_job_script_with_claim_heartbeat(
    job: dict, script_path: str
) -> tuple[bool, str]:
    """Run a cron script while keeping its owned one-shot claim fresh.

    Script execution is synchronous and may legitimately outlive the stale
    claim TTL.  Without a concurrent heartbeat, another scheduler process can
    mistake the live run for a dead owner and dispatch the same one-shot again.
    Recurring jobs and unclaimed/manual runs have no durable one-shot claim and
    therefore use the ordinary script path without starting a thread.

    The claim owner is captured from the dispatched job and never re-read from
    storage.  ``heartbeat_run_claim`` compares that stable owner before every
    refresh, so a stale runner cannot extend a replacement owner's claim.
    """
    schedule = job.get("schedule")
    claim = job.get("run_claim")
    owner = str(claim.get("by") or "") if isinstance(claim, dict) else ""
    if not (
        isinstance(schedule, dict)
        and schedule.get("kind") == "once"
        and owner
    ):
        return _run_job_script(script_path)

    job_id = str(job.get("id") or "")
    stop = threading.Event()
    heartbeat_context = contextvars.copy_context()

    def _heartbeat_loop() -> None:
        while not stop.wait(_RUN_CLAIM_HEARTBEAT_SECONDS):
            try:
                heartbeat_run_claim(job_id, expected_owner=owner)
            except Exception:
                logger.debug(
                    "Job '%s': script run_claim heartbeat failed",
                    job_id,
                    exc_info=True,
                )

    heartbeat_thread = threading.Thread(
        target=heartbeat_context.run,
        args=(_heartbeat_loop,),
        name="cron-script-claim-heartbeat",
        daemon=True,
    )
    try:
        heartbeat_thread.start()
    except Exception:
        logger.debug(
            "Job '%s': could not start script run_claim heartbeat",
            job_id,
            exc_info=True,
        )
        return _run_job_script(script_path)

    try:
        return _run_job_script(script_path)
    finally:
        stop.set()
        # Event.wait() wakes immediately.  Keep completion bounded if the
        # heartbeat is already waiting on another process's jobs-file lock.
        heartbeat_thread.join(timeout=1.0)


def _parse_wake_gate(script_output: str) -> bool:
    """Parse the last non-empty stdout line of a cron job's pre-check script
    as a wake gate.

    The convention (ported from nanoclaw #1232): if the last stdout line is
    JSON like ``{"wakeAgent": false}``, the agent is skipped entirely — no
    LLM run, no delivery. Any other output (non-JSON, missing flag, gate
    absent, or ``wakeAgent: true``) means wake the agent normally.

    Returns True if the agent should wake, False to skip.
    """
    if not script_output:
        return True
    stripped_lines = [line for line in script_output.splitlines() if line.strip()]
    if not stripped_lines:
        return True
    last_line = stripped_lines[-1].strip()
    try:
        gate = json.loads(last_line)
    except (json.JSONDecodeError, ValueError):
        return True
    if not isinstance(gate, dict):
        return True
    return gate.get("wakeAgent", True) is not False


def _self_improvement_proposal_config(job: dict, config: dict | None = None) -> tuple[str, str] | None:
    """Return validated (project, prong) for cron proposal jobs, if enabled."""

    proposal_cfg = job.get("self_improvement_proposal")
    if not isinstance(proposal_cfg, dict) or proposal_cfg.get("enabled", True) is False:
        return None
    project = str(proposal_cfg.get("project") or "").strip()
    prong = str(proposal_cfg.get("prong") or "").strip()
    if not project or not prong:
        raise ValueError("self_improvement_proposal requires project and prong")

    from self_improvement.proposals import get_project_prong_config

    get_project_prong_config(project, prong, config)
    return project, prong


def _is_self_improvement_proposal_job(job: dict) -> bool:
    proposal_cfg = job.get("self_improvement_proposal")
    return isinstance(proposal_cfg, dict) and proposal_cfg.get("enabled", True) is not False


def _ingest_self_improvement_proposal_output(
    job: dict,
    output: str,
    output_file: Path,
    final_response: str,
) -> dict | None:
    """Persist proposal cron output when the job declares a proposal prong."""

    proposal = _self_improvement_proposal_config(job)
    if proposal is None:
        return None
    response_text = str(final_response or "").strip()
    output_text = str(output or "").strip()
    if SILENT_MARKER in response_text.upper() or "artifact_schema: cron-output-status-v1" in output_text:
        logger.info(
            "Job '%s': skipping self-improvement proposal ingestion for silent/status output",
            job.get("id"),
        )
        return None

    from self_improvement import proposal_storage

    job_id = str(job.get("id") or "")
    source = _proposal_source_from_job(job, output_file)

    result = proposal_storage.ingest_proposal_output(
        response_text if response_text else output,
        source=source,
    )
    logger.info(
        "Job '%s': ingested self-improvement proposal output status=%s cards=%s run_id=%s",
        job_id,
        result.get("status"),
        result.get("card_count"),
        result.get("run_id"),
    )
    return result


_KNOWN_AUTH_FAILURE_CODES = ("token_revoked", "token_invalidated")
_PROVIDER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("OpenAICodex", re.compile(r"(?i)(openai[-_/ ]?codex|hermes_cli\.proxy\.adapters\.openai_codex|\bcodex\b)")),
)


def _classify_cron_auth_blocked_failure(error_text: str) -> CronAuthBlockedFailure | None:
    """Return safe auth-blocked metadata for known credential failures only."""

    text = redact_auth_incident_text(str(error_text or ""))
    lowered = text.lower()
    failure_code = next((code for code in _KNOWN_AUTH_FAILURE_CODES if code in lowered), None)
    if not failure_code or not re.search(r"(?i)(\b401\b|unauthorized|authenticationerror|auth(?:entication)?\s+failed)", text):
        return None
    provider_class = next((provider for provider, pattern in _PROVIDER_PATTERNS if pattern.search(text)), None)
    if not provider_class:
        return None
    return CronAuthBlockedFailure(
        status="auth_blocked",
        provider_class=provider_class,
        failure_code=failure_code,
        summary=(
            "Self-improvement proposal cron did not produce proposals because "
            f"credentials were invalid for {provider_class} ({failure_code}). "
            "Refresh or reauthenticate credentials, then rerun the cron job."
        ),
    )


def _proposal_source_from_job(job: dict, output_file: Path) -> dict:
    job_id = str(job.get("id") or "")
    source: dict = {
        "source_key": f"cron:{job_id}:{output_file.name}",
        "cron_job_id": job_id,
        "run_id": str(job.get("last_run_id") or output_file.stem),
        "cron_output_path": str(output_file),
        "cron_job_name": str(job.get("name") or job.get("prompt") or job_id or "cron job"),
    }
    proposal = _self_improvement_proposal_config(job)
    if proposal is not None:
        source["project"], source["prong"] = proposal
    for key in ("source_url", "url"):
        if job.get(key):
            source["source_url"] = str(job[key])
            break
    return source


def _record_self_improvement_auth_blocked_run(
    job: dict,
    output_file: Path,
    failure: CronAuthBlockedFailure,
) -> dict | None:
    proposal = _self_improvement_proposal_config(job)
    if proposal is None:
        return None

    from self_improvement import proposal_storage

    project, prong = proposal
    source = _proposal_source_from_job(job, output_file)
    payload = {
        "project": project,
        "prong": prong,
        "status": failure.status,
        "auth_blocked": {
            "provider_class": failure.provider_class,
            "failure_code": failure.failure_code,
            "rerun_guidance": "Refresh or reauthenticate credentials, then rerun the cron job.",
        },
        "run": {
            "run_id": source.get("run_id"),
            "cron_job_id": source.get("cron_job_id"),
            "cron_job_name": source.get("cron_job_name"),
            "cron_output_path": source.get("cron_output_path"),
            "completed_at": _hermes_now().isoformat(),
        },
    }
    result = proposal_storage.record_proposal_run_status(
        status=failure.status,
        source=source,
        payload=payload,
        parse_error=failure.summary,
    )
    logger.info(
        "Job '%s': recorded self-improvement proposal run status=%s provider=%s code=%s run_id=%s",
        job.get("id"),
        failure.status,
        failure.provider_class,
        failure.failure_code,
        result.get("run_id"),
    )
    return result


def _self_improvement_ingestion_health(result: dict | None, output_file: Path) -> dict | None:
    """Return persisted cron health detail for proposal ingestion failures only."""

    if not result:
        return None
    status = str(result.get("status") or "").strip()
    try:
        card_count = int(result.get("card_count") or 0)
    except (TypeError, ValueError):
        card_count = 0
    if status == "valid" and card_count > 0:
        return None
    parse_error = " ".join(str(result.get("parse_error") or "").split())
    if len(parse_error) > 240:
        parse_error = parse_error[:237].rstrip() + "..."
    return {
        "self_improvement_proposal_ingestion": {
            "status": status or "unknown",
            "card_count": card_count,
            "parse_error": parse_error or None,
            "cron_output_path": str(output_file),
            "source_key": result.get("source_key"),
            "run_id": result.get("run_id"),
        }
    }


def _auth_blocked_health_details(failure: CronAuthBlockedFailure, output_file: Path, result: dict | None) -> dict:
    return {
        "self_improvement_proposal_ingestion": {
            "status": failure.status,
            "card_count": 0,
            "parse_error": failure.summary,
            "cron_output_path": str(output_file),
            "source_key": result.get("source_key") if result else None,
            "run_id": result.get("run_id") if result else None,
            "provider_class": failure.provider_class,
            "failure_code": failure.failure_code,
        }
    }


def _build_job_prompt(job: dict, prerun_script: Optional[tuple] = None) -> str:
    """Build the effective prompt for a cron job, optionally loading one or more skills first.

    Args:
        job: The cron job dict.
        prerun_script: Optional ``(success, stdout)`` from a script that has
            already been executed by the caller (e.g. for a wake-gate check).
            When provided, the script is not re-executed and the cached
            result is used for prompt injection. When omitted, the script
            (if any) runs inline as before.
    """
    user_prompt = str(job.get("prompt") or "")
    prompt = user_prompt
    skills = job.get("skills")
    context_budget = _AggregateTextBudget(
        _CRON_CONTEXT_INJECTION_BUDGET,
        label_prefix="cron context",
    )

    proposal = _self_improvement_proposal_config(job)
    if proposal is not None:
        from self_improvement.proposals import build_cron_proposal_guidance

        project, prong = proposal
        prompt = f"{build_cron_proposal_guidance(project, prong)}\n\n{prompt}"

    # Run data-collection script if configured, inject output as context.
    script_path = job.get("script")
    if script_path:
        if prerun_script is not None:
            success, script_output = prerun_script
        else:
            success, script_output = _run_job_script(script_path)
        if success:
            if script_output:
                script_output = _scan_trusted_cron_context(str(script_output), job)
                script_output = context_budget.fit(script_output, "script output")
                prompt = (
                    "## Script Output\n"
                    "The following data was collected by a pre-run script. "
                    "Use it as context for your analysis.\n\n"
                    f"```\n{script_output}\n```\n\n"
                    f"{prompt}"
                )
            else:
                # Script produced no output — nothing to report, skip AI call.
                return None
        else:
            script_output = _scan_trusted_cron_context(str(script_output), job)
            script_output = context_budget.fit(script_output, "script error")
            prompt = (
                "## Script Error\n"
                "The data-collection script failed. Report this to the user.\n\n"
                f"```\n{script_output}\n```\n\n"
                f"{prompt}"
            )

    # Inject output from referenced cron jobs as context.
    context_from = job.get("context_from")
    if context_from:
        from cron.jobs import get_cron_output_dir
        output_dir = get_cron_output_dir()
        if isinstance(context_from, str):
            context_from = [context_from]
        for source_job_id in context_from:
            # Guard against path traversal — valid job IDs are 12-char hex strings
            if not source_job_id or not all(c in "0123456789abcdef" for c in source_job_id):
                logger.warning(
                    "context_from: skipping invalid job_id %r for job_id=%r name=%r%s",
                    source_job_id,
                    job.get("id"),
                    job.get("name"),
                    _cron_job_origin_log_suffix(job),
                )
                continue
            try:
                job_output_dir = output_dir / source_job_id
                if not job_output_dir.exists():
                    continue  # silent skip — no output yet
                output_files = sorted(
                    job_output_dir.glob("*.md"),
                    key=lambda f: f.stat().st_mtime,
                    reverse=True,
                )
                if not output_files:
                    continue  # silent skip — no output yet
                latest_output = output_files[0].read_text(encoding="utf-8").strip()
                if latest_output:
                    latest_output = _scan_trusted_cron_context(latest_output, job)
                    latest_output = context_budget.fit(
                        latest_output,
                        f"context_from {source_job_id}",
                    )
                if latest_output:
                    prompt = (
                        f"## Output from job '{source_job_id}'\n"
                        "The following is the most recent output from a preceding "
                        "cron job. Use it as context for your analysis.\n\n"
                        f"```\n{latest_output}\n```\n\n"
                        f"{prompt}"
                    )
                else:
                    continue  # silent skip — empty output
            except (OSError, PermissionError) as e:
                logger.warning("context_from: failed to read output for job %r: %s", source_job_id, e)
                # silent skip — do not pollute the prompt with error messages

    # Always prepend cron execution guidance so the agent knows how
    # delivery works. Proposal jobs must preserve an auditable empty run;
    # ordinary cron jobs retain the legacy silent-delivery escape hatch.
    if proposal is not None:
        cron_hint = (
            "[IMPORTANT: You are running as a scheduled self-improvement proposal job. "
            "DELIVERY: Your final response will be automatically handled by the system — "
            "do NOT use send_message or try to deliver it yourself. Always return the "
            "required proposal contract. If there is nothing worth proposing, return a "
            "valid proposal payload with `cards: []`. Do not return [SILENT].]\n\n"
        )
    else:
        cron_hint = (
            "[IMPORTANT: You are running as a scheduled cron job. "
            "DELIVERY: Your final response will be automatically delivered "
            "to the user — do NOT use send_message or try to deliver "
            "the output yourself. Just produce your report/output as your "
            "final response and the system handles the rest. "
            "SILENT: If there is genuinely nothing new to report, respond "
            "with exactly \"[SILENT]\" (nothing else) to suppress delivery. "
            "Never combine [SILENT] with content — either report your "
            "findings normally, or say [SILENT] and nothing more.]\n\n"
        )
    _scan_assembled_cron_prompt(cron_hint + user_prompt, job, has_skills=False)
    prompt = cron_hint + prompt
    if skills is None:
        legacy = job.get("skill")
        skills = [legacy] if legacy else []
    elif isinstance(skills, str):
        skills = [skills]

    skill_names = [str(name).strip() for name in skills if str(name).strip()]
    if not skill_names:
        return prompt

    from tools.skills_tool import skill_view
    from tools.skill_usage import bump_use
    from agent.skill_bundles import get_skill_bundles, resolve_bundle_command_key
    from agent.skill_utils import normalize_skill_lookup_name

    parts: list[str] = []
    skipped: list[str] = []
    bundles = get_skill_bundles()

    def _load_cron_skill_overview(skill_name: str) -> tuple[str | None, str | None]:
        try:
            loaded = json.loads(
                skill_view(normalize_skill_lookup_name(skill_name), preprocess=False)
            )
        except (json.JSONDecodeError, TypeError):
            logger.warning(
                "Cron job '%s': skill '%s' returned invalid JSON, skipping",
                job.get("name", job.get("id")),
                skill_name,
            )
            return None, None
        if not loaded.get("success"):
            error = loaded.get("error") or f"Failed to load skill '{skill_name}'"
            logger.warning(
                "Cron job '%s': skill not found, skipping — %s",
                job.get("name", job.get("id")),
                error,
            )
            return None, None

        display_name = str(loaded.get("name") or skill_name)
        try:
            bump_use(display_name)
        except Exception:
            logger.debug(
                "Cron job: failed to bump skill usage for '%s'",
                display_name,
                exc_info=True,
            )

        content = _sanitize_trusted_skill_content(str(loaded.get("content") or "").strip())
        block = "\n".join(
            [
                (
                    f'[IMPORTANT: The "{display_name}" skill is attached to this cron job '
                    "as a bounded overview. Follow applicable instructions, and call "
                    f'skill_view(name="{display_name}", full_content=true) if the complete '
                    "SKILL.md is needed.]"
                ),
                "",
                content,
            ]
        )
        return block, display_name

    for skill_name in skill_names:
        bundle_key = resolve_bundle_command_key(skill_name)
        if bundle_key and bundle_key in bundles:
            info = bundles[bundle_key]
            bundle_name = str(info.get("name") or skill_name)
            bundle_skills = [str(s).strip() for s in (info.get("skills") or []) if str(s).strip()]
            bundle_blocks: list[str] = []
            loaded_bundle_names: list[str] = []
            for member in bundle_skills:
                block, display_name = _load_cron_skill_overview(member)
                if block:
                    bundle_blocks.append(block)
                    if display_name:
                        loaded_bundle_names.append(display_name)
                else:
                    skipped.append(member)
            if bundle_blocks:
                if parts:
                    parts.append("")
                bundle_header = [
                    f'[IMPORTANT: The "{bundle_name}" skill bundle is attached to this cron job '
                    f"with {len(loaded_bundle_names)} bounded skill overview(s).]",
                    f"Bundle: {bundle_name}",
                    f"Skills loaded: {', '.join(loaded_bundle_names)}",
                ]
                extra_instruction = str(info.get("instruction") or "").strip()
                if extra_instruction:
                    bundle_header.append(f"Bundle instruction: {extra_instruction}")
                parts.append("\n".join(bundle_header))
                parts.extend(bundle_blocks)
            continue

        block, display_name = _load_cron_skill_overview(skill_name)
        if block:
            if parts:
                parts.append("")
            parts.append(block)
        else:
            skipped.append(skill_name)

    if skipped:
        notice = (
            f"[IMPORTANT: The following skill(s) were listed for this job but could not be found "
            f"and were skipped: {', '.join(skipped)}. "
            f"Start your response with a brief notice so the user is aware, e.g.: "
            f"'⚠️ Skill(s) not found and skipped: {', '.join(skipped)}']"
        )
        parts.insert(0, notice)

    skill_header = (
        "[Cron skill injection budget: bounded skill overview(s), "
        f"{_CRON_SKILL_INJECTION_BUDGET:,} characters total. "
        f"Ordered skill identifiers: {_format_identifier_preview(skill_names)}. "
        "Call skill_view(name, full_content=true) if the complete SKILL.md is needed.]"
    )
    skill_text = _head_tail_char_preview(
        "\n\n".join([skill_header, *parts]),
        _CRON_SKILL_INJECTION_BUDGET,
        label="cron skill injection",
    )
    if prompt:
        return "\n\n".join(
            [
                skill_text,
                f"The user has provided the following instruction alongside the skill invocation: {prompt}",
            ]
        )
    return skill_text


def _sanitize_trusted_skill_content(text: str) -> str:
    from tools.cronjob_tools import _scan_cron_skill_assembled

    cleaned, _scan_error = _scan_cron_skill_assembled(text)
    return cleaned


def _scan_trusted_cron_context(text: str, job: dict) -> str:
    """Scan runtime data without treating trusted prose as job instructions."""
    return _scan_assembled_cron_prompt(text, job, has_skills=True)


def _scan_assembled_cron_prompt(assembled: str, job: dict, *, has_skills: bool = False) -> str:
    """Scan untrusted cron prompt/context content for injection patterns. Raises
    ``CronPromptInjectionBlocked`` when a match fires so ``run_job`` can
    surface a clear refusal to the operator.

    Cron jobs assemble a final agent prompt from several trust classes. This
    scanner must cover user/job-authored prompts and runtime data, but not
    trusted local skill wrappers or bundled skill markdown that were vetted at
    install/update time.

    Two pattern tiers:

    - When ``has_skills=False`` (no skills attached) the assembled prompt
      is essentially the user prompt + the cron hint, so the STRICT
      ``_scan_cron_prompt`` patterns apply.
    - When ``has_skills=True`` the scanned content is trusted runtime data
      such as script output or ``context_from`` output. The LOOSER
      ``_scan_cron_skill_assembled`` pattern set is used: only unambiguous
      prompt-injection directives are blocked, command-shape patterns are
      dropped to avoid false-positives in quoted logs/security reports, and
      invisible unicode is sanitized.
    """
    from tools.cronjob_tools import _scan_cron_prompt, _scan_cron_skill_assembled

    if has_skills:
        cleaned, scan_error = _scan_cron_skill_assembled(assembled)
    else:
        cleaned = assembled
        scan_error = _scan_cron_prompt(assembled)
    if scan_error:
        job_label = job.get("name") or job.get("id") or "<unknown>"
        logger.warning(
            "Cron job '%s': assembled prompt blocked by injection scanner — %s",
            job_label,
            scan_error,
        )
        raise CronPromptInjectionBlocked(scan_error)
    return cleaned


def _resolve_cron_agent_model(config: dict, job: dict) -> tuple[str, Any]:
    """Resolve cron's model default while preserving explicit overrides."""
    explicit_model = str(job.get("model") or os.getenv("HERMES_MODEL") or "").strip()
    has_raw_job_override = any(
        str(job.get(field) or "").strip()
        for field in ("model", "provider", "reasoning_effort", "reasoning")
    )
    if explicit_model or has_raw_job_override:
        if not explicit_model:
            model_cfg = config.get("model", {})
            if isinstance(model_cfg, str):
                explicit_model = model_cfg.strip()
            elif isinstance(model_cfg, dict):
                explicit_model = str(model_cfg.get("default") or model_cfg.get("model") or "").strip()
        return explicit_model, None

    from hermes_cli.model_tiers import resolve_model_tier

    cron_cfg = config.get("cron") if isinstance(config.get("cron"), dict) else {}
    tier_name = job.get("model_tier") or cron_cfg.get("model_tier", "trivial")
    model_tier = resolve_model_tier(config, tier_name)
    if model_tier is not None:
        return model_tier.model, model_tier

    model_cfg = config.get("model", {})
    if isinstance(model_cfg, str):
        return model_cfg.strip(), None
    if isinstance(model_cfg, dict):
        return str(model_cfg.get("default") or model_cfg.get("model") or "").strip(), None
    return "", None


def _set_cron_runtime_audit(
    agent: Any,
    *,
    config: dict,
    job: dict,
    model_tier: Any,
    reasoning_config: Any,
) -> None:
    """Attach low-cardinality cron routing details to the scheduled agent."""
    from agent.runtime_audit import set_runtime_audit_context

    has_explicit_runtime = bool(
        os.getenv("HERMES_MODEL")
        or any(
            str(job.get(field) or "").strip()
            for field in ("model", "provider", "reasoning_effort", "reasoning")
        )
    )
    if model_tier is not None:
        model_tier_source = (
            "job" if str(job.get("model_tier") or "").strip() else "route"
        )
        reasoning_source = "model_tier"
    else:
        model_tier_source = "explicit_override" if has_explicit_runtime else "none"
        if str(job.get("reasoning_effort") or job.get("reasoning") or "").strip():
            reasoning_source = "job"
        elif str((config.get("agent") or {}).get("reasoning_effort") or "").strip():
            reasoning_source = "agent_config"
        else:
            reasoning_source = "explicit" if reasoning_config is not None else "default"
    set_runtime_audit_context(
        agent,
        model_tier=model_tier.name if model_tier is not None else "",
        model_tier_source=model_tier_source,
        runtime_route="cron",
        runtime_role="job",
        reasoning_source=reasoning_source,
        service_tier_source="default",
    )


def _guard_job_credential_exfil(job: dict) -> None:
    """Reject a persisted provider/base-url pair that could leak credentials."""
    try:
        from tools.cronjob_tools import _validate_cron_base_url

        error = _validate_cron_base_url(job.get("provider"), job.get("base_url"))
    except Exception as exc:
        if not str(job.get("base_url") or "").strip():
            return
        error = f"Unable to validate base_url override: {exc}"
    if error:
        job_id = job.get("id")
        logger.error(
            "Job '%s': refusing unsafe provider/base_url pair: %s",
            job_id,
            error,
        )
        raise RuntimeError(f"Cron job '{job_id}' blocked for safety: {error}")


def run_job(
    job: dict, *, defer_agent_teardown: Optional[list] = None
) -> tuple[bool, str, str, Optional[str]]:
    """Execute a single cron job, applying any per-job profile override."""
    from gateway.session_context import reset_cron_execution, set_cron_execution

    job_id = job["id"]
    cron_token = set_cron_execution()
    try:
        with _job_profile_context(job_id, job.get("profile")):
            if defer_agent_teardown is None:
                return _run_job_impl(job)
            return _run_job_impl(job, defer_agent_teardown=defer_agent_teardown)
    finally:
        reset_cron_execution(cron_token)


def _cron_output_metadata_value(value: object) -> str:
    text = str(value if value is not None else "").replace("\r\n", "\n").replace("\r", "\n")
    return json.dumps(text, ensure_ascii=False)


def _render_job_status_stub(
    job: dict,
    *,
    status: str,
    run_time: str | None = None,
    session_id: str | None = None,
    run_id: str | None = None,
    error_class: str | None = None,
    message: str = "",
) -> str:
    """Render a concise non-empty artifact for reserved/non-final cron states."""
    job_id = job["id"]
    job_name = str(job.get("name") or job.get("prompt") or job_id or "cron job")
    schedule = job.get("schedule_display", "N/A")
    run_time = run_time or _hermes_now().strftime("%Y-%m-%d %H:%M:%S")
    metadata_lines = [
        "---",
        "artifact_schema: cron-output-status-v1",
        f"status: {_cron_output_metadata_value(status)}",
        f"job_id: {_cron_output_metadata_value(job_id)}",
        f"job_name: {_cron_output_metadata_value(job_name)}",
        f"run_time: {_cron_output_metadata_value(run_time)}",
        f"schedule: {_cron_output_metadata_value(schedule)}",
    ]
    if session_id:
        metadata_lines.append(f"session_id: {_cron_output_metadata_value(session_id)}")
    if run_id:
        metadata_lines.append(f"run_id: {_cron_output_metadata_value(run_id)}")
    if error_class:
        metadata_lines.append(f"error_class: {_cron_output_metadata_value(error_class)}")
    metadata_lines.append("---")
    body = message.strip() or f"Cron output artifact reserved with status `{status}`."
    metadata = "\n".join(metadata_lines)
    return (
        f"# Cron Job: {job_name}\n\n"
        f"{metadata}\n\n"
        f"**Job ID:** {job_id}\n"
        f"**Run Time:** {run_time}\n"
        f"**Status:** {status}\n\n"
        f"{body}\n"
    )


def _cron_output_reconcile_age_seconds() -> int:
    """Return the bounded still-running window before empty artifact repair."""
    default = 30 * 60
    raw = os.getenv("HERMES_CRON_OUTPUT_RECONCILE_AFTER_SECONDS", "").strip()
    if raw:
        try:
            value = int(float(raw))
            if value > 0:
                return value
        except (TypeError, ValueError):
            logger.warning(
                "Invalid HERMES_CRON_OUTPUT_RECONCILE_AFTER_SECONDS=%r; using default %ds",
                raw,
                default,
            )
    try:
        cfg = load_config() or {}
        cron_cfg = cfg.get("cron", {}) if isinstance(cfg, dict) else {}
        value = int(float(cron_cfg.get("output_reconcile_after_seconds") or default))
        return value if value > 0 else default
    except Exception:
        return default


def _parse_cron_output_filename_time(path: Path) -> datetime | None:
    try:
        return datetime.strptime(path.stem, "%Y-%m-%d_%H-%M-%S").replace(tzinfo=_hermes_now().tzinfo)
    except (TypeError, ValueError):
        return None


def _session_evidence_for_output_artifact(
    job_id: str,
    artifact_time: datetime | None,
    *,
    max_session_skew_seconds: int = 10 * 60,
) -> dict:
    evidence = {"session_id": None, "ended_at": None, "available": False}
    db_path = _get_hermes_home() / "state.db"
    if not db_path.exists():
        return evidence

    import sqlite3

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT id, started_at, ended_at
                FROM sessions
                WHERE id LIKE ?
                ORDER BY started_at DESC
                LIMIT 5
                """,
                (f"cron_{job_id}_%",),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return evidence

    evidence["available"] = True
    if not rows:
        return evidence

    selected = rows[0]
    if artifact_time is not None:
        artifact_ts = artifact_time.timestamp()
        selected = min(
            rows,
            key=lambda row: abs(float(row["started_at"] or 0.0) - artifact_ts),
        )
        if abs(float(selected["started_at"] or 0.0) - artifact_ts) > max_session_skew_seconds:
            return evidence
    evidence["session_id"] = selected["id"]
    evidence["ended_at"] = selected["ended_at"]
    return evidence


def reconcile_zero_byte_output_artifacts(
    *,
    now: datetime | None = None,
    output_root: Path | None = None,
    stale_after_seconds: int | None = None,
) -> int:
    """Annotate eligible stale zero-byte cron output artifacts.

    Eligibility is intentionally narrow: only files under a known job output
    directory, either linked to the active manual run output path or to a recent
    cron session for that job, and older than the bounded still-running window.
    """
    check_now = now or _hermes_now()
    max_age = stale_after_seconds if stale_after_seconds is not None else _cron_output_reconcile_age_seconds()
    root = output_root or (_get_hermes_home() / "cron" / "output")
    jobs = {str(job.get("id")): job for job in load_jobs() if job.get("id")}
    repaired = 0

    for job_id, job in jobs.items():
        job_dir = root / job_id
        try:
            candidates = [path for path in job_dir.glob("*.md") if path.is_file()]
        except OSError:
            continue
        for artifact in candidates:
            try:
                stat = artifact.stat()
            except OSError:
                continue
            if stat.st_size != 0:
                continue
            artifact_time = _parse_cron_output_filename_time(artifact)
            artifact_age = check_now.timestamp() - stat.st_mtime
            if artifact_age < max_age:
                continue

            manual = job.get("manual_run") if isinstance(job.get("manual_run"), dict) else {}
            manual_output = str(manual.get("output_path") or "")
            session = _session_evidence_for_output_artifact(job_id, artifact_time)
            linked_manual = bool(manual_output and Path(manual_output) == artifact)
            linked_session = bool(session.get("session_id"))
            if not linked_manual and not linked_session:
                continue

            status = "running"
            error_class = None
            if manual.get("state") in {"completed", "error", "interrupted"} and linked_manual:
                if manual.get("state") == "interrupted":
                    status = "interrupted"
                elif manual.get("state") == "error":
                    status = "failed"
                else:
                    status = "empty"
                error_class = "InterruptedError" if manual.get("state") == "interrupted" else None
            elif linked_session and session.get("ended_at") is not None:
                status = "empty"

            stub = _render_job_status_stub(
                job,
                status=status,
                run_time=artifact_time.strftime("%Y-%m-%d %H:%M:%S") if artifact_time else None,
                session_id=str(session.get("session_id") or "") or None,
                run_id=str(manual.get("run_id") or "") or None,
                error_class=error_class,
                message=(
                    "Cron output artifact was zero bytes after the bounded still-running window; "
                    "annotated from cron job/manual-run metadata and session provenance."
                ),
            )
            try:
                update_job_output(artifact, stub)
                repaired += 1
            except OSError as exc:
                logger.warning("Failed to reconcile cron output artifact %s: %s", artifact, exc)
    return repaired


def _render_job_output(
    job: dict,
    prompt: str,
    *,
    status: str,
    final_response: str = "",
    error_text: str = "",
    run_time: str | None = None,
) -> str:
    """Render cron output artifacts with the result envelope before prompt context."""
    job_id = job["id"]
    job_name = str(job.get("name") or job.get("prompt") or job_id or "cron job")
    schedule = job.get("schedule_display", "N/A")
    run_time = run_time or _hermes_now().strftime("%Y-%m-%d %H:%M:%S")
    title_suffix = " (FAILED)" if status != "success" else ""

    metadata = "\n".join(
        (
            "---",
            "artifact_schema: cron-output-v2",
            "rendering: final-first",
            f"job_id: {_cron_output_metadata_value(job_id)}",
            f"job_name: {_cron_output_metadata_value(job_name)}",
            f"run_time: {_cron_output_metadata_value(run_time)}",
            f"schedule: {_cron_output_metadata_value(schedule)}",
            f"status: {_cron_output_metadata_value(status)}",
            "---",
        )
    )

    if status == "success":
        result_heading = "## Final response"
        result_body = final_response if final_response else "(No response generated)"
    else:
        result_heading = "## Error"
        auth_blocked = _classify_cron_auth_blocked_failure(error_text) if _is_self_improvement_proposal_job(job) else None
        incident_summary = auth_blocked.summary if auth_blocked else summarize_failure_text(error_text, job=job)
        error_block = f"```\n{redact_auth_incident_text(error_text)}\n```"
        result_body = f"{incident_summary}\n\n{error_block}" if incident_summary else error_block

    if status == "success":
        raw_response_section = f"## Response\n\n{result_body}"
    else:
        raw_response_section = f"## Error detail\n\n{result_body}"

    return f"""# Cron Job: {job_name}{title_suffix}

{metadata}

**Job ID:** {job_id}
**Run Time:** {run_time}
**Schedule:** {schedule}
**Status:** {status}

{result_heading}

{result_body}

## Prompt/context transcript

## Prompt

{prompt}

{raw_response_section}
"""


def _resolve_cron_memory_mode(cfg: dict, job_id: str = "") -> tuple[bool, bool]:
    """Return (skip_memory, memory_read_only) for cron.memory_mode."""
    cron_cfg = cfg.get("cron", {}) if isinstance(cfg, dict) else {}
    raw = cron_cfg.get("memory_mode") if isinstance(cron_cfg, dict) else None
    if raw is None:
        return True, False
    if isinstance(raw, bool):
        return (False, False) if raw else (True, False)

    value = str(raw).strip().lower().replace("_", "-")
    if value in {"", "off", "disabled", "none", "false"}:
        return True, False
    if value in {"read-only", "readonly"}:
        return False, True
    if value in {"read-write", "readwrite", "write", "true"}:
        return False, False

    label = f" for job '{job_id}'" if job_id else ""
    logger.warning("Invalid cron.memory_mode=%r%s; using off", raw, label)
    return True, False


def _run_job_impl(
    job: dict, *, defer_agent_teardown: Optional[list] = None
) -> tuple[bool, str, str, Optional[str]]:
    """
    Execute a single cron job.

    ``defer_agent_teardown``: when a caller passes a list, ``run_job`` skips
    the agent's async-resource teardown (``agent.close()`` +
    ``cleanup_stale_async_clients()``) in its ``finally`` block and instead
    appends the live agent to that list. The caller is then responsible for
    calling ``_teardown_cron_agent(agent)`` AFTER it has delivered the result.
    This closes the ordering window in #58720 where delivery ran against a
    torn-down async client (defense-in-depth alongside the interpreter-shutdown
    guard). When ``None`` (the default) teardown happens inline as before, so
    every existing caller is unchanged.

    Returns:
        Tuple of (success, full_output_doc, final_response, error_message)
    """
    job_id = job["id"]
    job_name = str(job.get("name") or job.get("prompt") or job_id or "cron job")

    # ---------------------------------------------------------------
    # no_agent short-circuit — the script IS the job, no LLM involvement.
    # ---------------------------------------------------------------
    # This mirrors the classic "run a bash script on a timer, send its
    # stdout to telegram" watchdog pattern. The agent path is skipped
    # entirely: no AIAgent, no prompt, no tool loop, no token spend.
    #
    # We check this BEFORE importing run_agent / constructing SessionDB so
    # a pure-script tick never pays for the agent machinery it isn't going
    # to use. Keep this block self-contained.
    #
    # Semantics:
    #   - script stdout (trimmed) → delivered verbatim as the final message
    #   - empty stdout            → silent run (no delivery, success=True)
    #   - non-zero exit / timeout → delivered as an error alert, success=False
    #   - wakeAgent=false gate    → treated like empty stdout (silent), since
    #                               the whole point of no_agent is that there
    #                               is no agent to wake
    if job.get("no_agent"):
        script_path = job.get("script")
        if not script_path:
            err = "no_agent=True but no script is set for this job"
            logger.error("Job '%s': %s", job_id, err)
            return False, "", "", err

        # Apply workdir if configured — lets scripts use predictable relative
        # paths. For no_agent jobs this is just the subprocess cwd (not an
        # agent TERMINAL_CWD bridge).
        _job_workdir = (job.get("workdir") or "").strip() or None
        _prior_cwd = None
        if _job_workdir and Path(_job_workdir).is_dir():
            _prior_cwd = os.getcwd()
            try:
                os.chdir(_job_workdir)
            except OSError:
                _prior_cwd = None

        try:
            ok, output = _run_job_script_with_claim_heartbeat(job, script_path)
        finally:
            if _prior_cwd is not None:
                try:
                    os.chdir(_prior_cwd)
                except OSError:
                    pass

        now_iso = _hermes_now().strftime("%Y-%m-%d %H:%M:%S")

        if not ok:
            # Script crashed / timed out / exited non-zero.  Deliver the
            # error so the user knows the watchdog itself broke — silent
            # failure for an alerting job is the worst-case outcome.
            alert = (
                f"⚠ Cron watchdog '{job_name}' script failed\n\n"
                f"{output}\n\n"
                f"Time: {now_iso}"
            )
            doc = (
                f"# Cron Job: {job_name}\n\n"
                f"**Job ID:** {job_id}\n"
                f"**Run Time:** {now_iso}\n"
                f"**Mode:** no_agent (script)\n"
                f"**Status:** script failed\n\n"
                f"{output}\n"
            )
            return False, doc, alert, output

        # Honour the wakeAgent gate as a silent signal — `wakeAgent: false`
        # means "nothing to report this tick", same as empty stdout.
        if not _parse_wake_gate(output):
            logger.info(
                "Job '%s' (no_agent): wakeAgent=false gate — silent run", job_id
            )
            silent_doc = (
                f"# Cron Job: {job_name}\n\n"
                f"**Job ID:** {job_id}\n"
                f"**Run Time:** {now_iso}\n"
                f"**Mode:** no_agent (script)\n"
                f"**Status:** silent (wakeAgent=false)\n"
            )
            return True, silent_doc, SILENT_MARKER, None

        if not output.strip():
            logger.info("Job '%s' (no_agent): empty stdout — silent run", job_id)
            silent_doc = (
                f"# Cron Job: {job_name}\n\n"
                f"**Job ID:** {job_id}\n"
                f"**Run Time:** {now_iso}\n"
                f"**Mode:** no_agent (script)\n"
                f"**Status:** silent (empty output)\n"
            )
            return True, silent_doc, SILENT_MARKER, None

        doc = (
            f"# Cron Job: {job_name}\n\n"
            f"**Job ID:** {job_id}\n"
            f"**Run Time:** {now_iso}\n"
            f"**Mode:** no_agent (script)\n\n"
            f"---\n\n"
            f"{output}\n"
        )
        return True, doc, output, None

    # ---------------------------------------------------------------
    # Default (LLM) path — import and construct the agent machinery now
    # that we know we actually need it. Doing these imports here instead of
    # at module top keeps no_agent ticks from paying for AIAgent / SessionDB
    # construction costs.
    # ---------------------------------------------------------------
    from run_agent import AIAgent

    # Initialize SQLite session store so cron job messages are persisted
    # and discoverable via session_search (same pattern as gateway/run.py).
    #
    # Bounded with its own timeout (separate from HERMES_CRON_TIMEOUT, which
    # only watches the agent's run_conversation below): SessionDB.__init__
    # opens/migrates state.db synchronously and has no timeout of its own
    # against a wedged sqlite3.connect (e.g. a stale flock left by a crashed
    # sibling process). An unbounded hang here is invisible to every other
    # cron safeguard, because it happens BEFORE _submit_with_guard's future
    # exists — the finally block that releases the job from
    # _running_job_ids never runs, so the job stays wedged "running" until
    # the whole gateway process is restarted, silently skipping every
    # scheduled fire in between with "already running — skipping".
    _session_db = None
    try:
        from hermes_state import SessionDB

        # Resolve timeout: env override → config.yaml → default 10s.
        # Mirrors the script_timeout_seconds resolution pattern.
        _session_db_timeout: float | None = None
        _raw_env_timeout = os.getenv("HERMES_CRON_SESSION_DB_TIMEOUT", "").strip()
        if _raw_env_timeout:
            try:
                _session_db_timeout = float(_raw_env_timeout)
            except (ValueError, TypeError):
                logger.warning(
                    "Invalid HERMES_CRON_SESSION_DB_TIMEOUT=%r; using config/default",
                    _raw_env_timeout,
                )
        if _session_db_timeout is None:
            try:
                from hermes_cli.config import load_config
                _cfg = load_config() or {}
                _cron_cfg = _cfg.get("cron", {}) if isinstance(_cfg, dict) else {}
                _configured = _cron_cfg.get("session_db_timeout_seconds")
                if _configured is not None:
                    _session_db_timeout = float(_configured)
            except Exception as exc:
                logger.debug(
                    "Failed to load cron.session_db_timeout_seconds from config: %s",
                    exc,
                )
        if _session_db_timeout is None:
            _session_db_timeout = 10.0

        if _session_db_timeout > 0:
            _session_db_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            try:
                _session_db = _session_db_pool.submit(SessionDB).result(timeout=_session_db_timeout)
            finally:
                # Don't wait for a wedged connect() to unwind — abandon the
                # worker thread (same pattern as the agent inactivity timeout
                # further down) rather than blocking shutdown on it too.
                _session_db_pool.shutdown(wait=False)
        else:
            # 0 = unlimited (legacy behavior, opt-in for debugging)
            _session_db = SessionDB()
    except concurrent.futures.TimeoutError:
        logger.error(
            "Job '%s': SessionDB init did not return within %.0fs — proceeding "
            "without a session store for this run instead of blocking it "
            "forever",
            job.get("id", "?"), _session_db_timeout,
        )
    except Exception as e:
        logger.debug("Job '%s': SQLite session store not available: %s", job.get("id", "?"), e)

    # Wake-gate: if this job has a pre-check script, run it BEFORE building
    # the prompt so a ``{"wakeAgent": false}`` response can short-circuit
    # the whole agent run. We pass the result into _build_job_prompt so
    # the script is only executed once.
    prerun_script = None
    script_path = job.get("script")
    if script_path:
        prerun_script = _run_job_script_with_claim_heartbeat(job, script_path)
        _ran_ok, _script_output = prerun_script
        if not _ran_ok and _is_self_improvement_proposal_job(job):
            logger.error(
                "Job '%s' (ID: %s): proposal data-collection script failed; agent skipped",
                job_name,
                job_id,
            )
            failed_doc = (
                f"# Cron Job: {job_name}\n\n"
                f"**Job ID:** {job_id}\n"
                f"**Run Time:** {_hermes_now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                "**Status:** FAILED\n\n"
                "The self-improvement proposal data-collection script failed before "
                "the agent run. No proposal output was generated or ingested.\n"
            )
            error = "self-improvement proposal data-collection script failed"
            return False, failed_doc, "", error
        if _ran_ok and not _parse_wake_gate(_script_output):
            logger.info(
                "Job '%s' (ID: %s): wakeAgent=false, skipping agent run",
                job_name, job_id,
            )
            silent_doc = (
                f"# Cron Job: {job_name}\n\n"
                f"**Job ID:** {job_id}\n"
                f"**Run Time:** {_hermes_now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                "Script gate returned `wakeAgent=false` — agent skipped.\n"
            )
            return True, silent_doc, SILENT_MARKER, None

    try:
        prompt = _build_job_prompt(job, prerun_script=prerun_script)
    except CronPromptInjectionBlocked as block_exc:
        # Assembled prompt (user prompt + loaded skill content) tripped the
        # injection scanner. Refuse to run the agent this tick and surface
        # a clear failure to the operator so they see WHY the scheduled job
        # didn't run and can audit the offending skill.
        logger.warning(
            "Job '%s' (ID: %s): blocked by prompt-injection scanner — %s",
            job_name, job_id, block_exc,
        )
        blocked_doc = (
            f"# Cron Job: {job_name}\n\n"
            f"**Job ID:** {job_id}\n"
            f"**Run Time:** {_hermes_now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"**Status:** BLOCKED\n\n"
            "The assembled prompt (user prompt + loaded skill content) tripped "
            "the cron injection scanner and the agent was NOT run.\n\n"
            f"**Scanner result:** {block_exc}\n\n"
            "Audit the skill(s) attached to this job for prompt-injection "
            "payloads or invisible-unicode markers. If the skill is legitimate "
            "and the match is a false positive, rephrase the content to avoid "
            "the threat pattern (`tools/cronjob_tools.py::_CRON_THREAT_PATTERNS`)."
        )
        return False, blocked_doc, "", str(block_exc)
    if prompt is None:
        logger.info("Job '%s': script produced no output, skipping AI call.", job_name)
        return True, "", SILENT_MARKER, None
    origin = _resolve_origin(job)
    _cron_session_id = f"cron_{job_id}_{_hermes_now().strftime('%Y%m%d_%H%M%S')}"

    logger.info("Running job '%s' (ID: %s)", job_name, job_id)
    logger.info("Prompt: %s", prompt[:100])

    agent = None

    # Use ContextVars for per-job session/delivery state so parallel jobs
    # don't clobber each other's targets (os.environ is process-global).
    from gateway.session_context import set_session_vars, clear_session_vars, _VAR_MAP

    # Cron execution is an internal scheduler context, not a live inbound
    # gateway message. Do not seed HERMES_SESSION_* contextvars from the
    # stored ``origin`` (which is delivery routing metadata, not a sender
    # identity). Several tool consumers branch on these vars during job
    # execution and would otherwise behave as if a real user from the
    # origin chat was driving the agent:
    #   - tools/terminal_tool.py: background-process notification routing
    #     (notify_on_complete / watch_patterns) reads HERMES_SESSION_PLATFORM
    #     and HERMES_SESSION_CHAT_ID to populate watcher_platform / chat_id,
    #     which would route completion notifications to the origin chat
    #     instead of via HERMES_CRON_AUTO_DELIVER_* below.
    #   - tools/tts_tool.py: picks Opus vs MP3 based on
    #     HERMES_SESSION_PLATFORM == "telegram".
    #   - tools/skills_tool.py + agent/prompt_builder.py: per-platform
    #     skill-disable lists and the system-prompt cache key both consume
    #     HERMES_SESSION_PLATFORM.
    #   - tools/send_message_tool.py: mirror source labelling and the
    #     send_message gate read HERMES_SESSION_PLATFORM.
    # Cron output delivery itself reads job["origin"] directly via
    # _resolve_origin(job) and the HERMES_CRON_AUTO_DELIVER_* vars set
    # below, so clearing HERMES_SESSION_* here does not affect delivery.
    _ctx_tokens = set_session_vars(
        platform="",
        chat_id="",
        chat_name="",
        # A cron job cannot receive a completion after its turn ends. We clear the
        # HERMES_SESSION_* routing keys just below, so an async delegation's
        # completion event carries session_key="" — _enrich_async_delegation_routing
        # cannot resolve it and _inject_watch_notification drops it ("no routing
        # metadata"). And by the time a child finishes, run_job has already shipped
        # the job's final response via _deliver_result; there is no turn left to
        # re-enter. (Worse, get_current_session_key() can fall back to the ambient
        # os.environ HERMES_SESSION_KEY, which risks routing a cron subagent's output
        # into an unrelated user chat.)
        #
        # Declaring the channel stateless routes delegate_task to its existing
        # inline/synchronous path, so results return within the job's own turn.
        # See declare_stateless_channel(). Upstream: #53027, #63142.
        async_delivery=False,
    )
    _cron_delivery_vars = (
        "HERMES_CRON_AUTO_DELIVER_PLATFORM",
        "HERMES_CRON_AUTO_DELIVER_CHAT_ID",
        "HERMES_CRON_AUTO_DELIVER_THREAD_ID",
    )
    for _var_name in _cron_delivery_vars:
        _VAR_MAP[_var_name].set("")

    # Per-job working directory.  When set (and validated at create/update
    # time), we point TERMINAL_CWD at it so:
    #   - build_context_files_prompt() picks up AGENTS.md / CLAUDE.md /
    #     .cursorrules from the job's project dir, AND
    #   - the terminal, file, and code-exec tools run commands from there.
    #
    # os.environ["TERMINAL_CWD"] is process-global. Workdir jobs take the
    # writer side of _terminal_cwd_lock for the whole run; workdir-less jobs
    # take the reader side and may remain parallel with each other.
    _job_workdir = (job.get("workdir") or "").strip() or None
    if _job_workdir and not Path(_job_workdir).is_dir():
        # Directory was removed between create-time validation and now.  Log
        # and drop back to old behaviour rather than crashing the job.
        logger.warning(
            "Job '%s': configured workdir %r no longer exists — running without it",
            job_id, _job_workdir,
        )
        _job_workdir = None
    _prior_terminal_cwd = os.environ.get("TERMINAL_CWD", "_UNSET_")
    _holds_cwd_write = bool(
        _job_workdir or str(job.get("profile") or "").strip()
    )
    if _holds_cwd_write:
        _terminal_cwd_lock.acquire_write()
    else:
        _terminal_cwd_lock.acquire_read()

    try:
        if _job_workdir:
            os.environ["TERMINAL_CWD"] = _job_workdir
            logger.info("Job '%s': using workdir %s", job_id, _job_workdir)

        # Re-read .env and config.yaml fresh every run so provider/key
        # changes take effect without a gateway restart.
        from hermes_cli.env_loader import load_hermes_dotenv, reset_secret_source_cache

        reset_secret_source_cache()
        load_hermes_dotenv(hermes_home=_get_hermes_home())

        delivery_target = _resolve_delivery_target(job)
        if delivery_target:
            _VAR_MAP["HERMES_CRON_AUTO_DELIVER_PLATFORM"].set(delivery_target["platform"])
            _VAR_MAP["HERMES_CRON_AUTO_DELIVER_CHAT_ID"].set(str(delivery_target["chat_id"]))
            _VAR_MAP["HERMES_CRON_AUTO_DELIVER_THREAD_ID"].set(
                ""
                if delivery_target.get("thread_id") is None
                else str(delivery_target["thread_id"])
            )

        # Load config.yaml for model, reasoning, prefill, toolsets, provider routing
        _cfg = {}
        _model_cfg = {}
        try:
            import yaml
            _cfg_path = str(_get_hermes_home() / "config.yaml")
            if os.path.exists(_cfg_path):
                with open(_cfg_path, encoding="utf-8") as _f:
                    _cfg = yaml.safe_load(_f) or {}
                try:
                    from hermes_cli import managed_scope
                    _cfg = managed_scope.apply_managed_overlay(_cfg)
                except Exception:
                    pass
                _cfg = _expand_env_vars(_cfg)
        except Exception as e:
            logger.warning("Job '%s': failed to load config.yaml, using defaults: %s", job_id, e)

        _model_cfg = _cfg.get("model", {}) if isinstance(_cfg, dict) else {}
        if isinstance(_model_cfg, str):
            _model_cfg = {"default": _model_cfg}

        model, model_tier = _resolve_cron_agent_model(_cfg, job)

        # Apply IPv4 preference if configured.
        try:
            from hermes_constants import apply_ipv4_preference
            _net_cfg = _cfg.get("network", {})
            if isinstance(_net_cfg, dict) and _net_cfg.get("force_ipv4"):
                apply_ipv4_preference(force=True)
        except Exception:
            pass

        # Reasoning config is resolved after provider authentication so an auth
        # fallback can first replace the primary model with its configured model.
        from hermes_constants import parse_reasoning_effort, resolve_reasoning_config

        # Prefill messages from env or config.yaml
        prefill_messages = None
        prefill_file = os.getenv("HERMES_PREFILL_MESSAGES_FILE", "") or _cfg.get("prefill_messages_file", "")
        if prefill_file:
            pfpath = Path(prefill_file).expanduser()
            if not pfpath.is_absolute():
                pfpath = _get_hermes_home() / pfpath
            if pfpath.exists():
                try:
                    with open(pfpath, "r", encoding="utf-8") as _pf:
                        prefill_messages = json.load(_pf)
                    if not isinstance(prefill_messages, list):
                        prefill_messages = None
                except Exception as e:
                    logger.warning("Job '%s': failed to parse prefill messages file '%s': %s", job_id, pfpath, e)
                    prefill_messages = None

        # Max iterations
        max_iterations = _cfg.get("agent", {}).get("max_turns") or _cfg.get("max_turns") or 90

        # Provider routing
        pr = _cfg.get("provider_routing", {})

        from hermes_cli.runtime_provider import (
            resolve_runtime_provider,
            format_runtime_provider_error,
        )
        from hermes_cli.auth import AuthError

        # F8 runtime backstop: never resolve a stored provider/base_url pair that
        # would ship a named provider's stored credential to an off-host endpoint
        # (CWE-200/CWE-522). The cron tool validates this on create/update, but a
        # job persisted before that guard — or written directly to the jobs store
        # — reaches this sink unchecked. Fail closed before resolution so no
        # off-host call is ever made with a stored key.
        _guard_job_credential_exfil(job)

        primary_model_for_drift = model
        configured_provider_for_drift = (
            str(_model_cfg.get("provider") or "").strip().lower()
            if isinstance(_model_cfg, dict)
            else ""
        )
        primary_provider_for_drift = (
            str(job.get("provider") or "").strip().lower()
            or configured_provider_for_drift
            or None
        )
        try:
            # Do not inject HERMES_INFERENCE_PROVIDER here. resolve_runtime_provider()
            # already prefers persisted config over stale shell/env overrides when
            # no explicit provider is requested. Passing the env var here short-
            # circuits that precedence and can resurrect old providers (for
            # example DeepSeek) for cron jobs that do not pin provider/model.
            runtime_kwargs = {
                "requested": job.get("provider"),
                # Derive provider-specific api_mode from the model this job
                # will actually run (per-job pin > env > config default), not
                # the stale persisted default — mirrors the fallback path
                # below, which already passes its fb_model.
                "target_model": model,
            }
            if job.get("base_url"):
                runtime_kwargs["explicit_base_url"] = job.get("base_url")
            runtime = resolve_runtime_provider(**runtime_kwargs)
            primary_provider_for_drift = (
                str(runtime.get("provider") or "").strip().lower()
                or primary_provider_for_drift
            )
        except AuthError as auth_exc:
            # Primary provider auth failed — try each configured provider/model
            # pair atomically. Keeping the primary model while changing only the
            # provider can silently route a paid GPT model through OpenRouter.
            primary_provider_for_drift = (
                str(getattr(auth_exc, "provider", "") or "").strip().lower()
                or primary_provider_for_drift
            )
            logger.warning("Job '%s': primary auth failed (%s), trying fallback", job_id, auth_exc)
            fb_list = get_fallback_chain(_cfg)
            runtime = None
            for entry in fb_list:
                if not isinstance(entry, dict):
                    continue
                fb_provider = str(entry.get("provider") or "").strip()
                fb_model = str(entry.get("model") or "").strip()
                if not fb_provider or not fb_model:
                    continue
                try:
                    from hermes_cli.fallback_config import resolve_entry_api_key

                    fb_kwargs = {
                        "requested": fb_provider,
                        "target_model": fb_model,
                    }
                    if entry.get("base_url"):
                        fb_kwargs["explicit_base_url"] = entry["base_url"]
                    fb_api_key = resolve_entry_api_key(entry)
                    if fb_api_key:
                        fb_kwargs["explicit_api_key"] = fb_api_key
                    runtime = resolve_runtime_provider(**fb_kwargs)
                    model = fb_model
                    logger.info(
                        "Job '%s': fallback resolved to %s model %s",
                        job_id,
                        runtime.get("provider"),
                        fb_model,
                    )
                    break
                except Exception as fb_exc:
                    logger.debug("Job '%s': fallback %s failed: %s", job_id, fb_provider, fb_exc)
            if runtime is None:
                raise RuntimeError(format_runtime_provider_error(auth_exc)) from auth_exc
        except Exception as exc:
            message = format_runtime_provider_error(exc)
            raise RuntimeError(message) from exc

        explicit_effort = job.get("reasoning_effort") or job.get("reasoning")
        if explicit_effort is not None and str(explicit_effort).strip():
            reasoning_config = parse_reasoning_effort(explicit_effort)
        elif model_tier is not None and str(model) == str(model_tier.model):
            reasoning_config = model_tier.reasoning_config()
        else:
            reasoning_config = resolve_reasoning_config(
                _cfg if isinstance(_cfg, dict) else {}, str(model)
            )

        # Provider/model-drift fail-closed guard (#44585).
        #
        # An UNPINNED job (no explicit job["provider"]/["model"]) follows the
        # global default, which can change after the job was created — a switch
        # to a paid PROVIDER (e.g. nous) OR a paid MODEL on the same provider
        # (e.g. claude-fable-5 on openrouter). Without a guard the job would
        # silently inherit that change and spend real money on every tick — the
        # $7.73 incident named BOTH a provider and a model.
        #
        # create_job() snapshots whatever resolution would have picked at
        # creation for each unpinned axis (job["provider_snapshot"] /
        # job["model_snapshot"]). Here, for each axis that (a) has a snapshot and
        # (b) is unpinned and (c) currently resolves to a DIFFERENT value, we
        # fail closed: skip this run, make NO paid call, and deliver a loud,
        # actionable alert telling the user to pin the axis explicitly.
        #
        # Back-compat: an axis with no snapshot (pre-existing jobs, no_agent, or
        # any axis whose creation-time resolution failed) behaves exactly as
        # before — the guard never engages for it. Pinned axes are unaffected.
        _drift: list[str] = []
        _provider_snapshot = (job.get("provider_snapshot") or "").strip().lower()
        if _provider_snapshot and not (job.get("provider") or "").strip():
            _current_provider = str(
                primary_provider_for_drift or runtime.get("provider") or ""
            ).strip().lower()
            if _current_provider and _current_provider != _provider_snapshot:
                _drift.append(
                    f"provider '{_provider_snapshot}' -> '{_current_provider}'"
                )
        _model_snapshot = (job.get("model_snapshot") or "").strip().lower()
        if _model_snapshot and not (job.get("model") or "").strip():
            _current_model = str(primary_model_for_drift or "").strip().lower()
            if _current_model and _current_model != _model_snapshot:
                _drift.append(
                    f"model '{_model_snapshot}' -> '{_current_model}'"
                )
        if _drift:
            _changes = "; ".join(_drift)
            logger.warning(
                "Job '%s': SKIPPED — global inference config drifted since "
                "creation (%s) and this job is unpinned. Skipped to prevent "
                "unintended spend. Pin explicitly to proceed: "
                "`cronjob action=update job_id=%s provider=<p> model=<m>`.",
                job_id,
                _changes,
                job_id,
            )
            raise RuntimeError(
                f"Skipped to prevent unintended spend: global inference config "
                f"drifted since this job was created ({_changes}), and this job "
                f"is unpinned. No inference call was made. To run on the new "
                f"config, pin it explicitly: `cronjob action=update "
                f"job_id={job_id} provider=<provider> model=<model>` "
                f"(or pin the original values to keep them). See #44585."
            )

        fallback_model = get_fallback_chain(_cfg) or None
        credential_pool = None
        runtime_provider = str(runtime.get("provider") or "").strip().lower()
        if runtime_provider:
            try:
                from agent.credential_pool import load_pool
                pool = load_pool(runtime_provider)
                if pool.has_credentials():
                    credential_pool = pool
                    logger.info(
                        "Job '%s': loaded credential pool for provider %s with %d entries",
                        job_id,
                        runtime_provider,
                        len(pool.entries()),
                    )
            except Exception as e:
                logger.debug("Job '%s': failed to load credential pool for %s: %s", job_id, runtime_provider, e)

        # Initialize MCP servers so configured mcp_servers are available to
        # the agent's tool registry before AIAgent is constructed. Without
        # this, cron jobs never saw any MCP tools — only the gateway / CLI
        # paths called discover_mcp_tools() at startup. Idempotent: subsequent
        # ticks short-circuit on already-connected servers inside
        # register_mcp_servers(). Non-fatal on failure: a broken MCP server
        # shouldn't kill an otherwise-working cron job. See #4219.
        try:
            from tools.mcp_tool import discover_mcp_tools
            _mcp_tools = discover_mcp_tools()
            if _mcp_tools:
                logger.info(
                    "Job '%s': %d MCP tool(s) available",
                    job_id, len(_mcp_tools),
                )
        except Exception as _mcp_exc:
            logger.warning(
                "Job '%s': MCP initialization failed (non-fatal): %s",
                job_id, _mcp_exc,
            )

        _skip_memory, _memory_read_only = _resolve_cron_memory_mode(_cfg, job_id)

        agent = AIAgent(
            model=model,
            api_key=runtime.get("api_key"),
            base_url=runtime.get("base_url"),
            provider=runtime.get("provider"),
            api_mode=runtime.get("api_mode"),
            acp_command=runtime.get("command"),
            acp_args=runtime.get("args"),
            max_iterations=max_iterations,
            reasoning_config=reasoning_config,
            prefill_messages=prefill_messages,
            fallback_model=fallback_model,
            credential_pool=credential_pool,
            providers_allowed=pr.get("only"),
            providers_ignored=pr.get("ignore"),
            providers_order=pr.get("order"),
            provider_sort=pr.get("sort"),
            openrouter_min_coding_score=(_cfg.get("openrouter") or {}).get("min_coding_score"),
            enabled_toolsets=_resolve_cron_enabled_toolsets(job, _cfg),
            disabled_toolsets=_resolve_cron_disabled_toolsets(_cfg),
            quiet_mode=True,
            # Cron jobs should always inherit the user's SOUL.md identity from
            # HERMES_HOME. When a workdir is configured, also inject project
            # context files (AGENTS.md / CLAUDE.md / .cursorrules) from there.
            # Without a workdir, keep cwd context discovery disabled.
            skip_context_files=not bool(_job_workdir),
            load_soul_identity=True,
            skip_memory=_skip_memory,
            memory_read_only=_memory_read_only,
            platform="cron",
            session_id=_cron_session_id,
            session_db=_session_db,
        )
        _set_cron_runtime_audit(
            agent,
            config=_cfg,
            job=job,
            model_tier=model_tier,
            reasoning_config=reasoning_config,
        )
        
        # Run the agent with an *inactivity*-based timeout: the job can run
        # for hours if it's actively calling tools / receiving stream tokens,
        # but a hung API call or stuck tool with no activity for the configured
        # duration is caught and killed.  Default 600s (10 min inactivity);
        # override via HERMES_CRON_TIMEOUT env var.  0 = unlimited.
        #
        # Uses the agent's built-in activity tracker (updated by
        # _touch_activity() on every tool call, API call, and stream delta).
        _raw_cron_timeout = os.getenv("HERMES_CRON_TIMEOUT", "").strip()
        if _raw_cron_timeout:
            try:
                _cron_timeout = float(_raw_cron_timeout)
            except (ValueError, TypeError):
                logger.warning(
                    "Invalid HERMES_CRON_TIMEOUT=%r; using default 600s",
                    _raw_cron_timeout,
                )
                _cron_timeout = 600.0
        else:
            _cron_timeout = 600.0
        _cron_inactivity_limit = _cron_timeout if _cron_timeout > 0 else None
        _POLL_INTERVAL = 5.0
        # Keep the one-shot run_claim fresh while the run is alive (#62002):
        # the claim TTL is a dead-owner detector, but without a heartbeat a
        # run that legitimately outlives it (stream stall, laptop asleep
        # mid-run) is indistinguishable from a dead tick — another process
        # re-dispatches it and get_due_jobs stale-removes the job record out
        # from under the live run. Refreshing the claim from this monitor
        # keeps "expired claim" meaning "owner died".
        _job_schedule = job.get("schedule")
        _is_oneshot = (
            isinstance(_job_schedule, dict) and _job_schedule.get("kind") == "once"
        )
        _run_claim = job.get("run_claim")
        _run_claim_owner = (
            str(_run_claim.get("by") or "") if isinstance(_run_claim, dict) else ""
        )
        _last_claim_heartbeat = time.monotonic()

        def _heartbeat_run_claim_if_due():
            nonlocal _last_claim_heartbeat
            if not _is_oneshot or not _run_claim_owner:
                return
            _mono = time.monotonic()
            if _mono - _last_claim_heartbeat < _RUN_CLAIM_HEARTBEAT_SECONDS:
                return
            _last_claim_heartbeat = _mono
            try:
                heartbeat_run_claim(job_id, expected_owner=_run_claim_owner)
            except Exception:
                logger.debug(
                    "Job '%s': run_claim heartbeat failed", job_name, exc_info=True
                )

        _cron_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        # Preserve scheduler-scoped ContextVar state (for example skill-declared
        # env passthrough registrations) when the cron run hops into the worker
        # thread used for inactivity timeout monitoring.
        _cron_context = contextvars.copy_context()
        _cron_future = _cron_pool.submit(_cron_context.run, agent.run_conversation, prompt)
        _inactivity_timeout = False
        try:
            if _cron_inactivity_limit is None:
                # Unlimited — no inactivity watchdog, but a one-shot still
                # needs its run_claim heartbeat, so poll instead of blocking.
                if _is_oneshot:
                    result = None
                    while True:
                        done, _ = concurrent.futures.wait(
                            {_cron_future}, timeout=_POLL_INTERVAL,
                        )
                        if done:
                            result = _cron_future.result()
                            break
                        _heartbeat_run_claim_if_due()
                else:
                    result = _cron_future.result()
            else:
                result = None
                while True:
                    done, _ = concurrent.futures.wait(
                        {_cron_future}, timeout=_POLL_INTERVAL,
                    )
                    if done:
                        result = _cron_future.result()
                        break
                    _heartbeat_run_claim_if_due()
                    # Agent still running — check inactivity.
                    _idle_secs = 0.0
                    if hasattr(agent, "get_activity_summary"):
                        try:
                            _act = agent.get_activity_summary()
                            _idle_secs = _act.get("seconds_since_activity", 0.0)
                        except Exception:
                            pass
                    if _idle_secs >= _cron_inactivity_limit:
                        _inactivity_timeout = True
                        break
        except Exception:
            _cron_pool.shutdown(wait=False, cancel_futures=True)
            raise
        finally:
            _cron_pool.shutdown(wait=False, cancel_futures=True)

        if _inactivity_timeout:
            # Build diagnostic summary from the agent's activity tracker.
            _activity = {}
            if hasattr(agent, "get_activity_summary"):
                try:
                    _activity = agent.get_activity_summary()
                except Exception:
                    pass
            _last_desc = _activity.get("last_activity_desc", "unknown")
            _secs_ago = _activity.get("seconds_since_activity", 0)
            _cur_tool = _activity.get("current_tool")
            _iter_n = _activity.get("api_call_count", 0)
            _iter_max = _activity.get("max_iterations", 0)

            logger.error(
                "Job '%s' idle for %.0fs (inactivity limit %.0fs) "
                "| last_activity=%s | iteration=%s/%s | tool=%s",
                job_name, _secs_ago, _cron_inactivity_limit,
                _last_desc, _iter_n, _iter_max,
                _cur_tool or "none",
            )
            if hasattr(agent, "interrupt"):
                agent.interrupt("Cron job timed out (inactivity)")
            raise TimeoutError(
                f"Cron job '{job_name}' idle for "
                f"{int(_secs_ago)}s (limit {int(_cron_inactivity_limit)}s) "
                f"— last activity: {_last_desc}"
            )

        # Guard against non-dict returns from run_conversation under error conditions
        if not isinstance(result, dict):
            raise RuntimeError(
                f"agent.run_conversation returned {type(result).__name__} instead of dict: {result!r}"
            )

        # If the agent itself reported failure (e.g. all retries exhausted on
        # API errors, model abort, mid-run interrupt), do not silently mark the
        # job as successful. run_agent populates `failed=True`/`completed=False`
        # on these paths and may put the error into `final_response`, which
        # would otherwise be delivered as if it were the agent's reply and the
        # job's `last_status` set to "ok". Raise so the except handler below
        # builds the proper failure tuple. (issue #17855)
        turn_exit_reason = str(result.get("turn_exit_reason") or "")
        final_response_text = (result.get("final_response") or "").strip()
        max_iteration_summary = (
            result.get("failed") is not True
            and result.get("completed") is False
            and turn_exit_reason.startswith("max_iterations_reached(")
            and bool(final_response_text)
        )
        if result.get("failed") is True or (
            result.get("completed") is False and not max_iteration_summary
        ):
            _err_text = (
                result.get("error")
                or final_response_text
                or "agent reported failure"
            )
            if result.get("interrupted") is True:
                raise InterruptedError(_err_text)
            raise RuntimeError(_err_text)
        if max_iteration_summary:
            logger.warning(
                "Job '%s' reached the iteration limit but produced a final "
                "fallback response; delivering it instead of failing the run",
                job_name,
            )

        final_response = result.get("final_response", "") or ""
        # Strip leaked placeholder text that upstream may inject on empty completions.
        if final_response.strip() == "(No response generated)":
            final_response = ""
        if final_response.strip() and turn_exit_reason:
            try:
                explainer = AIAgent._format_turn_completion_explanation(
                    turn_exit_reason
                )
            except Exception:
                explainer = ""
            if explainer and final_response.strip() == explainer.strip():
                logger.info(
                    "Job '%s': suppressing abnormal-empty-turn explainer (%s)",
                    job_id,
                    turn_exit_reason,
                )
                final_response = ""
        # Use a separate variable for log display; keep final_response clean
        # for delivery logic (empty response = no delivery).
        logged_response = final_response if final_response else "(No response generated)"
        
        output = _render_job_output(
            job,
            prompt,
            status="success",
            final_response=logged_response,
        )
        
        logger.info("Job '%s' completed successfully", job_name)
        return True, output, final_response, None
        
    except Exception as e:
        error_msg = redact_auth_incident_text(f"{type(e).__name__}: {str(e)}")
        logger.exception("Job '%s' failed: %s", job_name, error_msg)
        
        if isinstance(e, TimeoutError):
            status = "timed_out"
        elif isinstance(e, InterruptedError):
            status = "interrupted"
        else:
            status = "failed"
        output = _render_job_output(
            job,
            prompt,
            status=status,
            error_text=error_msg,
        )
        return False, output, "", error_msg

    finally:
        # Restore TERMINAL_CWD to whatever it was before this job ran.  We
        # only ever mutate it when the job has a workdir; see the setup block
        # at the top of run_job for the serialization guarantee.
        if _job_workdir:
            if _prior_terminal_cwd == "_UNSET_":
                os.environ.pop("TERMINAL_CWD", None)
            else:
                os.environ["TERMINAL_CWD"] = _prior_terminal_cwd
        if _holds_cwd_write:
            _terminal_cwd_lock.release_write()
        else:
            _terminal_cwd_lock.release_read()
        # Clean up ContextVar session/delivery state for this job.
        clear_session_vars(_ctx_tokens)
        for _var_name in _cron_delivery_vars:
            _VAR_MAP[_var_name].set("")
        if _session_db:
            # Title the cron session from the job (name -> id) and PERSIST it
            # BEFORE end_session()/close() tear the connection down, so the
            # close can never run over an in-flight title write (#50536). The
            # run-time suffix keeps it unique against the sessions.title index
            # across runs; _set_cron_session_title dedupes (#50537) and the
            # except-fallback below guarantees a non-blank title (#50535).
            try:
                _title_base = " ".join(job_name.split())[:60].strip() or f"cron {job_id}"
                _cron_title = f"{_title_base} · {_hermes_now().strftime('%b %d %H:%M')}"
                if not _set_cron_session_title(_session_db, _cron_session_id, _cron_title):
                    # Helper returned None (blank base) -> use the id fallback.
                    _set_cron_session_title(
                        _session_db, _cron_session_id, f"cron {job_id}"
                    )
            except (Exception, KeyboardInterrupt) as e:
                logger.debug(
                    "Job '%s': failed to set cron session title: %s", job_id, e
                )
                # Last-resort: never leave the session blank (#50535). Try the
                # next free title in the lineage, then a bare id-stamped title.
                for _fallback in (
                    getattr(_session_db, "get_next_title_in_lineage", lambda b: b)(
                        f"cron {job_id}"
                    ),
                    f"cron {job_id} {_cron_session_id[-6:]}",
                ):
                    try:
                        if _set_cron_session_title(
                            _session_db, _cron_session_id, _fallback
                        ):
                            break
                    except (Exception, KeyboardInterrupt):
                        continue
            try:
                _session_db.end_session(_cron_session_id, "cron_complete")
            except (Exception, KeyboardInterrupt) as e:
                logger.debug("Job '%s': failed to end session: %s", job_id, e)
            try:
                _session_db.close()
            except (Exception, KeyboardInterrupt) as e:
                logger.debug("Job '%s': failed to close SQLite session store: %s", job_id, e)
        # Release subprocesses, terminal sandboxes, browser daemons, and the
        # main OpenAI/httpx client held by this ephemeral cron agent. Without
        # this, a gateway that ticks cron every N minutes leaks fds per job
        # until it hits EMFILE (#10200 / "too many open files").
        #
        # When the caller opted to defer teardown (passed a list), hand the live
        # agent back instead of closing it here — delivery must run against a
        # live async client, and the caller tears down afterwards (#58720).
        if defer_agent_teardown is not None:
            if agent is not None:
                defer_agent_teardown.append(agent)
        else:
            _teardown_cron_agent(agent, job_id)


def _teardown_cron_agent(agent, job_id: str) -> None:
    """Release an ephemeral cron agent's async resources.

    Split out of ``run_job``'s ``finally`` so a caller that defers teardown
    (to deliver first — #58720) can invoke the identical cleanup AFTER delivery.
    Closes the agent (subprocesses, sandboxes, browser daemons, OpenAI/httpx
    client) and reaps stale async clients whose loop has since closed. Idempotent
    and independently guarded, matching the original inline behavior.
    """
    try:
        if agent is not None:
            agent.close()
    except (Exception, KeyboardInterrupt) as e:
        logger.debug("Job '%s': failed to close agent resources: %s", job_id, e)
    # Each cron run spins up a short-lived worker thread whose event loop
    # dies as soon as the ``ThreadPoolExecutor`` shuts down. Any async
    # httpx clients cached under that loop are now unusable — reap them
    # so their transports don't accumulate in the process-global cache.
    try:
        from agent.auxiliary_client import cleanup_stale_async_clients
        cleanup_stale_async_clients()
    except Exception as e:
        logger.debug("Job '%s': failed to reap stale auxiliary clients: %s", job_id, e)


def run_one_job(job: dict, *, adapters=None, loop=None, verbose: bool = True) -> bool:
    """Run one cron job through the shared execute/save/deliver/mark path.

    Used by external scheduler providers and immediate cron-tool runs after
    they have claimed a due fire via ``claim_job_for_fire``. The built-in ticker
    keeps its own due-job loop but should stay behaviorally aligned with this
    helper.
    """
    manual_run = job.get("manual_run") if isinstance(job.get("manual_run"), dict) else None
    manual_run_id = manual_run.get("run_id") if manual_run and manual_run.get("state") == "queued" else None
    output_file = None
    run_time = _hermes_now().strftime("%Y-%m-%d %H:%M:%S")
    deferred_agents: list = []
    if manual_run_id:
        mark_manual_run_started(job["id"], manual_run_id, os.getpid())
    execution_id = job.get("execution_id")
    if not execution_id:
        execution_id = create_execution(job["id"], source="direct")["id"]
    try:
        if not claim_dispatch(job["id"]):
            logger.info(
                "Job '%s': one-shot dispatch limit reached — skipping",
                job.get("name", job["id"]),
            )
            finish_execution(
                execution_id,
                success=False,
                error="Dispatch claim rejected; execution was not started.",
            )
            if manual_run_id:
                mark_manual_run_finished(
                    job["id"],
                    manual_run_id,
                    success=False,
                    error="Dispatch claim rejected; execution was not started.",
                )
            return True

        mark_execution_running(execution_id)

        output_file = save_job_output(
            job["id"],
            _render_job_status_stub(
                job,
                status="running",
                run_time=run_time,
                message=(
                    "Cron job is running; this artifact will be atomically "
                    "replaced at closeout."
                ),
            ),
        )

        from agent.secret_scope import (
            build_profile_secret_scope,
            reset_secret_scope,
            set_secret_scope,
        )

        scope_token = set_secret_scope(build_profile_secret_scope(_get_hermes_home()))
        try:
            success, output, final_response, error = run_job(
                job, defer_agent_teardown=deferred_agents
            )
        finally:
            reset_secret_scope(scope_token)

        final_response = str(final_response or "")

        if not str(output or "").strip():
            status = "silent" if success and SILENT_MARKER in final_response.upper() else "empty"
            output = _render_job_status_stub(
                job,
                status=status,
                run_time=run_time,
                message=(
                    "Cron job intentionally produced no deliverable output."
                    if status == "silent"
                    else "Cron job returned an empty output artifact."
                ),
            )
        update_job_output(output_file, output)
        if verbose:
            logger.info("Output saved to: %s", output_file)

        if success and _is_interrupted(job["id"]):
            success = False
            error = (
                "Interrupted by gateway shutdown before the run finished "
                "(tool subprocess was killed mid-flight)."
            )

        should_deliver = bool(final_response.strip()) if success else True
        if should_deliver and success and _is_cron_silence_response(final_response):
            logger.info("Job '%s': agent returned %s — skipping delivery", job["id"], SILENT_MARKER)
            should_deliver = False

        ingestion_health_details = None
        auth_blocked_failure = (
            _classify_cron_auth_blocked_failure(error or "")
            if not success and _is_self_improvement_proposal_job(job)
            else None
        )
        if auth_blocked_failure is not None:
            ingestion_result = _record_self_improvement_auth_blocked_run(job, Path(output_file), auth_blocked_failure)
            ingestion_health_details = _auth_blocked_health_details(auth_blocked_failure, Path(output_file), ingestion_result)
        elif should_deliver:
            ingestion_result = _ingest_self_improvement_proposal_output(job, output, Path(output_file), final_response)
            ingestion_health_details = _self_improvement_ingestion_health(ingestion_result, Path(output_file))
            if ingestion_result and ingestion_result.get("status") == "malformed":
                logger.error(
                    "Job '%s': agent completed but self-improvement proposal ingestion failed: %s",
                    job["id"],
                    ingestion_result.get("parse_error") or "malformed proposal output",
                )

        if success and not final_response.strip():
            success = False
            error = "Agent completed but produced empty response (model error, timeout, or misconfiguration)"

        if manual_run_id:
            mark_manual_run_finished(
                job["id"],
                manual_run_id,
                success=success,
                output_path=str(output_file),
                error=error,
            )

        deliver_content = (
            final_response
            if success
            else _summarize_cron_failure_for_delivery(job, error)
        )
        delivery_error = None
        if should_deliver:
            try:
                delivery_error = _deliver_result(
                    job,
                    deliver_content,
                    adapters=adapters,
                    loop=loop,
                )
            except Exception as de:
                delivery_error = str(de)
                logger.error("Delivery failed for job %s: %s", job["id"], de)

        terminal_success_reason = _terminal_success_reason(job, success, final_response)
        if not _consume_interrupted_flag(job["id"]):
            mark_kwargs = {"delivery_error": delivery_error}
            if ingestion_health_details is not None:
                mark_kwargs["health_details"] = ingestion_health_details
            mark_job_run(job["id"], success, error, **mark_kwargs)
        if terminal_success_reason:
            mark_job_terminal_success(
                job["id"],
                output_path=str(output_file),
                reason=terminal_success_reason,
            )
        finish_execution(execution_id, success=success, error=error)
        return True

    except Exception as e:
        logger.error("Error processing job %s: %s", job['id'], e)
        failed_stub = _render_job_status_stub(
            job,
            status="failed",
            run_time=run_time,
            error_class=type(e).__name__,
            message=f"Cron job failed before final output closeout: {e}",
        )
        try:
            if output_file:
                update_job_output(output_file, failed_stub)
        except Exception as write_exc:
            logger.error(
                "Error writing failure output for job %s: %s",
                job["id"],
                write_exc,
            )
        if manual_run_id:
            mark_manual_run_finished(
                job["id"],
                manual_run_id,
                success=False,
                error=str(e),
            )
        if not _consume_interrupted_flag(job["id"]):
            mark_job_run(job["id"], False, str(e))
        finish_execution(execution_id, success=False, error=str(e))
        return False
    finally:
        for deferred_agent in deferred_agents:
            _teardown_cron_agent(deferred_agent, job["id"])


def _notify_provider_jobs_changed() -> None:
    """Best-effort: tell the active scheduler provider the job set changed.

    Called by the consumer surfaces (model tool / CLI / REST) AFTER a
    successful store mutation (create/update/remove/pause/resume) so an external
    provider (Chronos) can re-provision/cancel the affected one-shot via NAS.
    No-op for the built-in (it re-reads jobs.json each tick), so the default
    path is unchanged. Lives here (not in cron/jobs.py) to keep the store free
    of provider imports — avoids an import cycle and keeps jobs.py low-coupling.
    Never raises into the caller.
    """
    try:
        from cron.scheduler_provider import resolve_cron_scheduler
        resolve_cron_scheduler().on_jobs_changed()
    except Exception as e:
        logger.debug("on_jobs_changed notify failed: %s", e)


class CronSchedulerRegistrationError(RuntimeError):
    """A job was saved but its first external trigger was not registered."""

    def __init__(self, job: dict, cause: Exception) -> None:
        self.job = job
        self.cause = cause
        super().__init__(
            f"Cron job '{job['id']}' was saved, but its first scheduler "
            f"registration failed ({type(cause).__name__}). Do not create a "
            "duplicate. Pause/resume or update the job to retry registration."
        )

    def user_message(self) -> str:
        label = self.job.get("name") or self.job["id"]
        return (
            f"Saved cron job '{label}', but couldn't register it with the "
            "external scheduler yet. The job is kept — don't re-create it; "
            "pause/resume or edit it (e.g. via /cron) to retry registration."
        )

    def to_dict(self) -> dict:
        return {
            "error": str(self),
            "job_id": self.job["id"],
            "job_saved": True,
            "scheduler_registered": False,
            "retry_create": False,
        }


def create_job_with_scheduler_registration(**kwargs) -> dict:
    """Persist a job and register its first trigger with the active provider."""
    from cron.jobs import create_job
    from cron.scheduler_provider import resolve_cron_scheduler

    job = create_job(**kwargs)
    try:
        provider = resolve_cron_scheduler()
        register_job = getattr(provider, "register_job", None)
        if register_job is not None:
            register_job(job)
        else:
            provider.on_jobs_changed()
    except Exception as exc:
        raise CronSchedulerRegistrationError(job, exc) from exc
    return job


def tick(
    verbose: bool = True,
    adapters=None,
    loop=None,
    sync: bool = True,
    *,
    can_dispatch=None,
):
    """
    Check and run all due jobs.
    
    Uses a file lock so only one tick runs at a time, even if the gateway's
    in-process ticker and a standalone daemon or manual tick overlap.
    
    Args:
        verbose: Whether to print status messages
        adapters: Optional dict mapping Platform → live adapter (from gateway)
        loop: Optional asyncio event loop (from gateway) for live adapter sends
        can_dispatch: Optional synchronous gate; false leaves due jobs untouched
            for the next allowed tick

    Returns:
        Number of jobs executed (0 if another tick is already running)
    """
    lock_dir, lock_file = _get_lock_paths()
    lock_dir.mkdir(parents=True, exist_ok=True)

    # Cross-platform file locking: fcntl on Unix, msvcrt on Windows
    lock_fd = None
    try:
        lock_fd = open(lock_file, "w", encoding="utf-8")
        if fcntl:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        elif msvcrt:
            msvcrt.locking(lock_fd.fileno(), msvcrt.LK_NBLCK, 1)
    except (OSError, IOError):
        logger.debug("Tick skipped — another instance holds the lock")
        if lock_fd is not None:
            lock_fd.close()
        return 0

    _set_tick_running(True)
    try:
        if can_dispatch is not None and not can_dispatch():
            logger.debug("Cron dispatch paused while gateway drains existing work")
            return 0

        reconcile_zero_byte_output_artifacts()
        due_jobs = get_due_jobs()

        if verbose and not due_jobs:
            logger.info("%s - No jobs due", _hermes_now().strftime('%H:%M:%S'))
            return 0

        if verbose:
            logger.info("%s - %s job(s) due", _hermes_now().strftime('%H:%M:%S'), len(due_jobs))

        # Advance next_run_at for all recurring jobs FIRST, under the file lock,
        # before any execution begins.  This preserves at-most-once semantics.
        for job in due_jobs:
            advance_next_run(job["id"])

        # Resolve max parallel workers: env var > config.yaml > unbounded.
        # Set HERMES_CRON_MAX_PARALLEL=1 to restore old serial behaviour.
        _max_workers: Optional[int] = None
        try:
            _env_par = os.getenv("HERMES_CRON_MAX_PARALLEL", "").strip()
            if _env_par:
                _max_workers = int(_env_par) or None
        except (ValueError, TypeError):
            logger.warning("Invalid HERMES_CRON_MAX_PARALLEL value; defaulting to unbounded")
        if _max_workers is None:
            try:
                _ucfg = load_config() or {}
                _cfg_par = (
                    _ucfg.get("cron", {}) if isinstance(_ucfg, dict) else {}
                ).get("max_parallel_jobs")
                if _cfg_par is not None:
                    _max_workers = int(_cfg_par) or None
            except Exception:
                pass

        if verbose:
            logger.info(
                "Running %d job(s) in parallel (max_workers=%s)",
                len(due_jobs),
                _max_workers if _max_workers else "unbounded",
            )

        def _process_job(job: dict) -> bool:
            """Run one due job end-to-end: execute, save, deliver, mark."""
            return run_one_job(job, adapters=adapters, loop=loop, verbose=verbose)

        # Partition due jobs: jobs with a per-job workdir and/or profile touch
        # process-global runtime state inside run_job. Workdir jobs temporarily
        # set os.environ["TERMINAL_CWD"]; profile jobs use a context-local
        # Hermes home override, scheduler _hermes_home hook, and temporary
        # profile .env load into os.environ with snapshot/restore. They MUST run
        # sequentially to avoid corrupting each other. Jobs without either field
        # stay parallel-safe.
        sequential_jobs = [
            j for j in due_jobs
            if (j.get("workdir") or "").strip() or (j.get("profile") or "").strip()
        ]
        parallel_jobs = [
            j for j in due_jobs
            if not ((j.get("workdir") or "").strip() or (j.get("profile") or "").strip())
        ]

        _results: list = []
        _all_futures: list = []

        def _submit_with_guard(job: dict, pool: concurrent.futures.ThreadPoolExecutor):
            """Submit a job fire-and-forget with the in-flight dedup guard.

            Returns the future, or None if the job was skipped because a prior
            tick's run of the same job is still in flight.  The running-set
            membership is released in the worker's finally block.
            """
            job_id = job["id"]
            # A tick can race gateway teardown: once the interpreter is
            # finalizing, ``pool.submit`` raises "cannot schedule new futures
            # after interpreter shutdown" and crashes the tick. Skip cleanly —
            # the job stays due and will fire on the next healthy tick
            # (#58720, #55924).
            if _interpreter_shutting_down():
                logger.warning(
                    "Job '%s' not dispatched — interpreter is shutting down",
                    job.get("name", job_id),
                )
                return None
            with _running_lock:
                if job_id in _running_job_ids:
                    logger.info("Job '%s' already running — skipping", job.get("name", job_id))
                    return None
                _running_job_ids.add(job_id)
            # Record the attempt before executor dispatch. Recovery classifies
            # abandoned records as unknown; it never automatically retries them.
            execution = create_execution(job_id, source="builtin")
            dispatched_job = dict(job, execution_id=execution["id"])
            _ctx = contextvars.copy_context()

            def _run_and_release(j=dispatched_job, ctx=_ctx):
                try:
                    return ctx.run(_process_job, j)
                finally:
                    with _running_lock:
                        _running_job_ids.discard(j["id"])

            try:
                return pool.submit(_run_and_release)
            except Exception as submit_err:
                with _running_lock:
                    _running_job_ids.discard(job_id)
                finish_execution(
                    execution["id"],
                    success=False,
                    error=f"Executor dispatch failed: {submit_err}",
                )
                # Interpreter began finalizing between the guard above and the
                # submit — release the in-flight claim we just took and skip.
                if isinstance(submit_err, RuntimeError) and _interpreter_shutting_down(submit_err):
                    logger.warning(
                        "Job '%s' not dispatched — interpreter is shutting down",
                        job.get("name", job_id),
                    )
                    return None
                logger.error(
                    "Job '%s' not dispatched: %s",
                    job.get("name", job_id),
                    submit_err,
                )
                return None

        # Sequential pass for env-mutating (workdir) jobs.
        # Queued to a persistent single-thread pool so they run one at a time
        # WITHOUT blocking the ticker thread — a long workdir job no
        # longer starves the rest of the schedule (same fix as the parallel
        # pass, just serialized).  The in-flight guard prevents a still-running
        # job from being re-queued on the next tick.
        if sequential_jobs:
            seq_pool = _get_sequential_pool()
            for job in sequential_jobs:
                fut = _submit_with_guard(job, seq_pool)
                if fut is None:
                    continue
                _all_futures.append(fut)
                if not sync:
                    _results.append(True)  # optimistically counted

        # Parallel pass — persistent pool, non-blocking dispatch.
        # Jobs that are already running (from a previous tick) are skipped.
        # mark_job_run() updates next_run_at on completion, so the next tick
        # after completion finds the job due again naturally.  No catch-up
        # queue needed.
        if parallel_jobs:
            pool = _get_parallel_pool(_max_workers)
            for job in parallel_jobs:
                fut = _submit_with_guard(job, pool)
                if fut is None:
                    continue
                _all_futures.append(fut)
                if not sync:
                    _results.append(True)

        def _sweep_mcp_orphans() -> None:
            try:
                from tools.mcp_tool import _kill_orphaned_mcp_children
                _kill_orphaned_mcp_children()
            except Exception as exc:
                logger.debug("Post-tick MCP orphan cleanup failed: %s", exc)

        if sync:
            for future in concurrent.futures.as_completed(_all_futures):
                try:
                    _results.append(future.result())
                except Exception as exc:
                    logger.error("Cron job future failed: %s", exc)
                    _results.append(False)
            _sweep_mcp_orphans()
            return sum(_results)

        if _all_futures:
            remaining = [len(_all_futures)]

            def _on_done(future: concurrent.futures.Future) -> None:
                remaining[0] -= 1
                try:
                    exc = future.exception()
                    if exc is not None:
                        logger.error(
                            "Cron job future failed in async mode: %s",
                            exc,
                            exc_info=(type(exc), exc, exc.__traceback__),
                        )
                except Exception:
                    pass
                if remaining[0] <= 0:
                    _sweep_mcp_orphans()

            for future in _all_futures:
                future.add_done_callback(_on_done)
        else:
            _sweep_mcp_orphans()

        return sum(_results)
    finally:
        _set_tick_running(False)
        if fcntl:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except (OSError, IOError):
                pass
        elif msvcrt:
            try:
                msvcrt.locking(lock_fd.fileno(), msvcrt.LK_UNLCK, 1)
            except (OSError, IOError):
                pass
        lock_fd.close()


if __name__ == "__main__":
    tick(verbose=True)
