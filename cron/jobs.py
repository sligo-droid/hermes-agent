"""
Cron job storage and management.

Jobs are stored in ~/.hermes/cron/jobs.json
Output is saved to ~/.hermes/cron/output/{job_id}/{timestamp}.md
"""

import copy
import contextlib
from contextvars import ContextVar
from dataclasses import dataclass
import json
import logging
import shutil
import sqlite3
import tempfile
import threading
import time
import os
import re
import uuid

try:
    import fcntl
except ImportError:  # pragma: no cover - non-Unix
    fcntl = None
try:
    import msvcrt
except ImportError:  # pragma: no cover - non-Windows
    msvcrt = None
from datetime import datetime, timedelta
from pathlib import Path
from hermes_constants import get_hermes_home
from typing import Optional, Dict, List, Any, Union, Tuple

logger = logging.getLogger(__name__)

from hermes_time import now as _hermes_now
from utils import atomic_replace

try:
    from croniter import croniter
    HAS_CRONITER = True
except ImportError:
    HAS_CRONITER = False

# =============================================================================
# Configuration
# =============================================================================

HERMES_DIR = get_hermes_home().resolve()
# These constants remain the default-profile fallback and a compatibility
# surface for existing callers/tests. Cross-profile callers must scope paths
# with use_cron_store() instead of mutating them process-wide.
CRON_DIR = HERMES_DIR / "cron"
JOBS_FILE = CRON_DIR / "jobs.json"

# In-process lock protecting load_jobs→modify→save_jobs cycles.
# Required when tick() runs jobs in parallel threads — without this,
# concurrent mark_job_run / advance_next_run calls can clobber each other.
_jobs_file_lock = threading.RLock()
_jobs_lock_state = threading.local()

# Upper bound on waiting for the cross-process .jobs.lock flock (#60703).
# Every cron function in the process funnels through _jobs_lock(), and the
# flock is taken while holding the process-wide RLock — so an unbounded wait
# on a lock held by a wedged sibling process silently freezes the ticker
# heartbeat and every job forever.  30s is orders of magnitude above any
# legitimate critical section (field updates only) while keeping the ticker's
# worst-case stall well under one status-alarm threshold.
_JOBS_LOCK_TIMEOUT_SECONDS = 30.0
OUTPUT_DIR = CRON_DIR / "output"
TICKER_HEARTBEAT_FILE = CRON_DIR / "ticker_heartbeat"
TICKER_SUCCESS_FILE = CRON_DIR / "ticker_last_success"
ONESHOT_GRACE_SECONDS = 120

@dataclass(frozen=True)
class _CronStorePaths:
    cron_dir: Path
    jobs_file: Path
    output_dir: Path


_cron_store_override: ContextVar[Optional[_CronStorePaths]] = ContextVar(
    "cron_store_override",
    default=None,
)


# Import-time snapshot of the compatibility constants, so deliberate
# re-pointing of the module surface (monkeypatched CRON_DIR/JOBS_FILE/
# OUTPUT_DIR — the documented escape hatch existing tests/embedders use)
# is distinguishable from the constants merely being stale.
_IMPORT_STORE = _CronStorePaths(CRON_DIR, JOBS_FILE, OUTPUT_DIR)


def _current_cron_store() -> _CronStorePaths:
    """Return paths pinned to this execution context's profile.

    Precedence, most explicit first:

    1. an active use_cron_store() override (ContextVar);
    2. deliberately re-pointed module constants — if CRON_DIR/JOBS_FILE/
       OUTPUT_DIR no longer match their import-time values, someone chose
       the documented process-wide compatibility surface; honor it;
    3. the ACTIVE profile home, resolved fresh via get_hermes_home()
       (context-local override, then the HERMES_HOME env var) — so a test
       or embedder that re-points HERMES_HOME after this module was
       imported reads/writes ITS OWN store, not whatever jobs.json the
       import happened to freeze (the filed incident: fixtures that patched
       the env too late silently rewrote the user's real jobs file);
    4. the import-time constants (home unchanged since import — the common
       path, returned unchanged).
    """
    override = _cron_store_override.get()
    if override is not None:
        return override
    live_constants = _CronStorePaths(CRON_DIR, JOBS_FILE, OUTPUT_DIR)
    if live_constants != _IMPORT_STORE:
        return live_constants
    home = get_hermes_home().resolve()
    if home == HERMES_DIR:
        return live_constants
    cron_dir = home / "cron"
    return _CronStorePaths(cron_dir, cron_dir / "jobs.json", cron_dir / "output")


@contextlib.contextmanager
def use_cron_store(home: Union[str, Path]):
    """Route cron storage to ``home`` without mutating process globals."""
    cron_dir = Path(home).expanduser().resolve() / "cron"
    token = _cron_store_override.set(
        _CronStorePaths(
            cron_dir=cron_dir,
            jobs_file=cron_dir / "jobs.json",
            output_dir=cron_dir / "output",
        )
    )
    try:
        yield
    finally:
        _cron_store_override.reset(token)


def get_cron_output_dir() -> Path:
    """Return the output directory for the active cron store context."""
    return _current_cron_store().output_dir


# Fallback stale-recovery window for a one-shot's running-claim (#59229) when
# the cron inactivity timeout is disabled (HERMES_CRON_TIMEOUT=0 → unlimited),
# in which case no finite run bound exists to derive from. Also acts as the
# floor for the derived value so a very short configured timeout can't make the
# claim expire mid-run.
ONESHOT_RUN_CLAIM_TTL_SECONDS = 1800

# The derived TTL is the cron inactivity timeout times this headroom multiplier.
# A healthy run clears its claim via mark_job_run() long before the TTL; the
# TTL only recovers a claim left by a tick that DIED mid-run. HERMES_CRON_TIMEOUT
# is an *inactivity* limit, not a wall-clock cap — a job that keeps producing
# output legitimately runs past it — so the multiplier gives comfortable
# headroom over any healthy run before we treat a claim as stale.
_ONESHOT_RUN_CLAIM_TTL_HEADROOM = 3

_DEFAULT_CRON_INACTIVITY_TIMEOUT = 600.0


def _oneshot_run_claim_ttl_seconds() -> float:
    """Resolve the one-shot running-claim stale-recovery TTL.

    Derived from ``HERMES_CRON_TIMEOUT`` (the cron inactivity timeout the
    scheduler enforces on each run) so the safety valve tracks how long a run
    is actually allowed to go quiet, instead of a magic constant:

    - unset / invalid → default 600s inactivity limit → TTL = 1800s
    - ``0`` (unlimited runs) → no finite bound to derive from → fall back to
      ``ONESHOT_RUN_CLAIM_TTL_SECONDS``
    - positive N → ``max(N * headroom, ONESHOT_RUN_CLAIM_TTL_SECONDS)`` so a
      tiny configured timeout can never expire a claim mid-run.
    """
    raw = os.getenv("HERMES_CRON_TIMEOUT", "").strip()
    timeout = _DEFAULT_CRON_INACTIVITY_TIMEOUT
    if raw:
        try:
            timeout = float(raw)
        except (ValueError, TypeError):
            timeout = _DEFAULT_CRON_INACTIVITY_TIMEOUT
    if timeout <= 0:
        # Unlimited runs — cannot bound; use the fixed fallback floor.
        return float(ONESHOT_RUN_CLAIM_TTL_SECONDS)
    return max(
        timeout * _ONESHOT_RUN_CLAIM_TTL_HEADROOM,
        float(ONESHOT_RUN_CLAIM_TTL_SECONDS),
    )


def _job_running_in_this_process(job_id: str) -> bool:
    """Return True when the scheduler in THIS process is still running ``job_id``.

    Direct liveness signal for stale-entry recovery (#62002): the run_claim
    TTL alone cannot distinguish "the claiming tick died" from "the run is
    alive but slow" — a run stalled on network I/O (or a laptop that slept
    mid-run) legitimately outlives the TTL. The in-process ticker and the run
    share this process, so the scheduler's running set settles the common
    single-gateway case without any claim-age guesswork.

    Imported lazily: the scheduler imports this module at load, so a
    module-level import here would be circular.
    """
    try:
        from cron.scheduler import get_running_job_ids
        return job_id in get_running_job_ids()
    except Exception:
        logger.warning(
            "Cron running-set liveness check failed for job %r; keeping the "
            "entry to avoid deleting a possibly live one-shot run",
            job_id,
            exc_info=True,
        )
        return True


def _jobs_lock_file() -> Path:
    """Return the advisory lock path for the current cron directory."""
    return _current_cron_store().cron_dir / ".jobs.lock"


@contextlib.contextmanager
def _jobs_lock():
    """Serialize a load_jobs→modify→save_jobs critical section.

    Combines the in-process threading lock (cheap mutual exclusion between
    the gateway's parallel tick threads) with a cross-process advisory file
    lock on ``<cron dir>/.jobs.lock`` (mutual exclusion between the gateway process
    and standalone ``hermes`` CLI invocations, which previously shared no lock
    at all — a `cron pause` could be silently clobbered by a concurrent
    gateway write, leaving a "paused" job still firing).

    The flock is blocking, but every critical section that uses it is short
    (field updates only — no agent execution), so contention resolves in
    milliseconds. If neither fcntl nor msvcrt is available the manager still
    provides in-process locking, matching the historical behaviour.

    Nested calls in the same thread reuse the held lock so legacy callers that
    invoke save_jobs() inside a broader mutation section don't deadlock or try
    to reacquire the advisory file lock.
    """
    depth = getattr(_jobs_lock_state, "depth", 0)
    if depth:
        _jobs_lock_state.depth = depth + 1
        try:
            yield
        finally:
            _jobs_lock_state.depth -= 1
        return

    with _jobs_file_lock:
        _jobs_lock_state.depth = 1
        lock_fd = None
        try:
            try:
                ensure_dirs()
                lock_fd = open(_jobs_lock_file(), "a+", encoding="utf-8")
                lock_fd.seek(0)
                if fcntl is not None:
                    # Bounded acquisition (#60703): a plain blocking
                    # fcntl.flock(LOCK_EX) here has NO timeout, and it is
                    # taken while holding the process-wide _jobs_file_lock
                    # RLock above.  If another process wedges while holding
                    # .jobs.lock (e.g. an old gateway draining through a
                    # restart), a single blocked acquirer freezes EVERY cron
                    # function in this process — including the ticker's
                    # get_due_jobs() — silently and forever: the heartbeat
                    # file stops updating and all jobs stop firing with no
                    # error logged.  Poll LOCK_NB against a deadline instead;
                    # on timeout, log loudly and fall through to the same
                    # in-process-only degraded mode used when locking is
                    # unavailable.  A briefly-torn cross-process write is
                    # strictly better than a permanently dead scheduler.
                    _deadline = time.monotonic() + _JOBS_LOCK_TIMEOUT_SECONDS
                    while True:
                        try:
                            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except (OSError, IOError):
                            if time.monotonic() >= _deadline:
                                logger.error(
                                    "Timed out after %.0fs waiting for the cron "
                                    "jobs lock (%s) — another process is holding "
                                    "it. Proceeding with in-process locking only "
                                    "so the scheduler stays alive (#60703).",
                                    _JOBS_LOCK_TIMEOUT_SECONDS,
                                    _jobs_lock_file(),
                                )
                                try:
                                    lock_fd.close()
                                except OSError:
                                    pass
                                lock_fd = None
                                break
                            time.sleep(0.1)
                elif msvcrt is not None:
                    getattr(msvcrt, "locking")(lock_fd.fileno(), getattr(msvcrt, "LK_LOCK"), 1)
            except (OSError, IOError) as e:
                # Never let a locking failure take down cron writes — fall back to
                # in-process-only protection (still held via _jobs_file_lock).
                logger.warning("jobs.json cross-process lock unavailable (%s); "
                               "proceeding with in-process lock only", e)
            try:
                yield
            finally:
                if lock_fd is not None:
                    try:
                        if fcntl is not None:
                            fcntl.flock(lock_fd, fcntl.LOCK_UN)
                        elif msvcrt is not None:
                            getattr(msvcrt, "locking")(lock_fd.fileno(), getattr(msvcrt, "LK_UNLCK"), 1)
                    except (OSError, IOError):
                        pass
                    finally:
                        lock_fd.close()
        finally:
            _jobs_lock_state.depth = 0
# Fields on a cron job that must never change after creation. ``id`` is used
# as a filesystem path component under ``OUTPUT_DIR``; allowing it to be
# updated lets an unsafe value (``../escape``, absolute path, nested) leak
# into output writes/deletes.
_IMMUTABLE_JOB_FIELDS = frozenset({"id"})


def _job_output_dir(job_id: str) -> Path:
    """Resolve a job's output directory, rejecting any path-escape attempt.

    Job IDs are filesystem path components under ``OUTPUT_DIR``. A legacy or
    crafted ID containing ``..``, absolute paths, or nested separators would
    allow output writes/deletes to escape the cron output sandbox. Reject
    anything that isn't a single safe path component.
    """
    text = str(job_id or "").strip()
    if not text or text in {".", ".."} or "/" in text or "\\" in text:
        raise ValueError(f"Invalid cron job id for output path: {job_id!r}")
    if Path(text).is_absolute() or Path(text).drive:
        raise ValueError(f"Invalid cron job id for output path: {job_id!r}")
    return _current_cron_store().output_dir / text


def _normalize_skill_list(skill: Optional[str] = None, skills: Optional[Any] = None) -> List[str]:
    """Normalize legacy/single-skill and multi-skill inputs into a unique ordered list."""
    if skills is None:
        raw_items = [skill] if skill else []
    elif isinstance(skills, str):
        raw_items = [skills]
    else:
        raw_items = list(skills)

    normalized: List[str] = []
    for item in raw_items:
        text = str(item or "").strip()
        if text and text not in normalized:
            normalized.append(text)
    return normalized


def _apply_skill_fields(job: Dict[str, Any]) -> Dict[str, Any]:
    """Return a job dict with canonical `skills` and legacy `skill` fields aligned."""
    normalized = dict(job)
    skills = _normalize_skill_list(normalized.get("skill"), normalized.get("skills"))
    normalized["skills"] = skills
    normalized["skill"] = skills[0] if skills else None
    return normalized


def _coerce_job_text(value: Any, fallback: str = "") -> str:
    """Coerce legacy/hand-edited nullable cron fields to strings for readers."""
    if value is None:
        return fallback
    return str(value)


def _schedule_display_for_job(job: Dict[str, Any]) -> str:
    display = _coerce_job_text(job.get("schedule_display")).strip()
    if display:
        return display

    schedule = job.get("schedule")
    if isinstance(schedule, dict):
        for key in ("display", "value", "expr", "run_at"):
            text = _coerce_job_text(schedule.get(key)).strip()
            if text:
                return text
    elif schedule is not None:
        return str(schedule)

    return "?"


def _normalize_job_record(job: Dict[str, Any]) -> Dict[str, Any]:
    """Return a read-safe cron job shape for UI/API/tool/scheduler consumers.

    Older or hand-edited jobs can have nullable fields like ``prompt``,
    ``name``, or ``schedule_display``.  Keep storage untouched on read, but
    ensure consumers never crash while formatting or running those records.
    """
    normalized = _apply_skill_fields(job)
    job_id = _coerce_job_text(normalized.get("id"), "unknown")
    prompt = _coerce_job_text(normalized.get("prompt"))
    normalized["id"] = job_id
    normalized["prompt"] = prompt

    name = _coerce_job_text(normalized.get("name")).strip()
    if not name:
        script = _coerce_job_text(normalized.get("script")).strip()
        label_source = (
            prompt
            or (normalized["skills"][0] if normalized.get("skills") else "")
            or script
            or job_id
            or "cron job"
        )
        name = label_source[:50].strip() or "cron job"
    normalized["name"] = name
    normalized["schedule_display"] = _schedule_display_for_job(normalized)

    state = _coerce_job_text(normalized.get("state")).strip()
    if not state:
        state = "scheduled" if normalized.get("enabled", True) else "paused"
    normalized["state"] = state

    profile = _coerce_job_text(normalized.get("profile")).strip()
    normalized["profile"] = profile or None
    normalized["disable_on_terminal_success"] = bool(normalized.get("disable_on_terminal_success", False))
    normalized["last_terminal_output_path"] = normalized.get("last_terminal_output_path") or None

    return normalized


def _secure_dir(path: Path):
    """Set directory to owner-only access (0700). No-op on Windows."""
    try:
        os.chmod(path, 0o700)
    except (OSError, NotImplementedError):
        pass  # Windows or other platforms where chmod is not supported


def _secure_file(path: Path):
    """Set file to owner-only read/write (0600). No-op on Windows."""
    try:
        if path.exists():
            os.chmod(path, 0o600)
    except (OSError, NotImplementedError):
        pass


def _refresh_cron_paths_from_home() -> None:
    global HERMES_DIR, CRON_DIR, JOBS_FILE, OUTPUT_DIR, TICKER_HEARTBEAT_FILE, TICKER_SUCCESS_FILE

    current_home = get_hermes_home().resolve()
    if current_home == HERMES_DIR:
        return

    old_cron_dir = HERMES_DIR / "cron"
    old_jobs_file = old_cron_dir / "jobs.json"
    old_output_dir = old_cron_dir / "output"
    old_heartbeat_file = old_cron_dir / "ticker_heartbeat"
    old_success_file = old_cron_dir / "ticker_last_success"

    HERMES_DIR = current_home
    new_cron_dir = HERMES_DIR / "cron"
    cron_dir_was_default = CRON_DIR == old_cron_dir
    if cron_dir_was_default:
        CRON_DIR = new_cron_dir

    # Preserve explicit test/runtime monkeypatches, but do not let a temporary
    # CRON_DIR monkeypatch poison the core jobs path when HERMES_HOME changes
    # during the same worker process. Default dependents follow the new home;
    # custom dependents remain custom until their monkeypatch restores them.
    dependent_base = new_cron_dir if cron_dir_was_default else new_cron_dir
    if JOBS_FILE == old_jobs_file:
        JOBS_FILE = dependent_base / "jobs.json"
    if OUTPUT_DIR == old_output_dir:
        OUTPUT_DIR = dependent_base / "output"
    if TICKER_HEARTBEAT_FILE == old_heartbeat_file:
        TICKER_HEARTBEAT_FILE = dependent_base / "ticker_heartbeat"
    if TICKER_SUCCESS_FILE == old_success_file:
        TICKER_SUCCESS_FILE = dependent_base / "ticker_last_success"


def ensure_dirs():
    """Ensure cron directories exist with secure permissions."""
    store = _current_cron_store()
    store.cron_dir.mkdir(parents=True, exist_ok=True)
    store.output_dir.mkdir(parents=True, exist_ok=True)
    _secure_dir(store.cron_dir)
    _secure_dir(store.output_dir)


def _atomic_write_epoch(path: Path, value: float) -> None:
    ensure_dirs()
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix='.tmp', prefix='.hb_')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(str(value))
            f.flush()
            os.fsync(f.fileno())
        atomic_replace(tmp_path, path)
        _secure_file(path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def record_ticker_heartbeat(success: bool = False, now: Optional[datetime] = None) -> None:
    """Record scheduler liveness, and success when a tick completes cleanly."""
    try:
        epoch = (now or _hermes_now()).timestamp()
        _atomic_write_epoch(TICKER_HEARTBEAT_FILE, epoch)
        if success:
            _atomic_write_epoch(TICKER_SUCCESS_FILE, epoch)
    except Exception:
        return


def _ticker_age(path: Path, now: Optional[datetime] = None) -> Optional[float]:
    try:
        raw = path.read_text(encoding='utf-8').strip()
        if not raw:
            return None
        current = (now or _hermes_now()).timestamp()
        return max(0.0, current - float(raw))
    except Exception:
        return None


def get_ticker_heartbeat_age(now: Optional[datetime] = None) -> Optional[float]:
    return _ticker_age(TICKER_HEARTBEAT_FILE, now=now)


def get_ticker_success_age(now: Optional[datetime] = None) -> Optional[float]:
    return _ticker_age(TICKER_SUCCESS_FILE, now=now)


# =============================================================================
# Schedule Parsing
# =============================================================================

def parse_duration(s: str) -> int:
    """
    Parse duration string into minutes.
    
    Examples:
        "30m" → 30
        "2h" → 120
        "1d" → 1440
    """
    s = s.strip().lower()
    match = re.match(r'^(\d+)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)$', s)
    if not match:
        raise ValueError(f"Invalid duration: '{s}'. Use format like '30m', '2h', or '1d'")
    
    value = int(match.group(1))
    unit = match.group(2)[0]  # First char: m, h, or d
    
    multipliers = {'m': 1, 'h': 60, 'd': 1440}
    return value * multipliers[unit]


def parse_schedule(schedule: str) -> Dict[str, Any]:
    """
    Parse schedule string into structured format.
    
    Returns dict with:
        - kind: "once" | "interval" | "cron"
        - For "once": "run_at" (ISO timestamp)
        - For "interval": "minutes" (int)
        - For "cron": "expr" (cron expression)
    
    Examples:
        "30m"              → once in 30 minutes
        "2h"               → once in 2 hours
        "every 30m"        → recurring every 30 minutes
        "every 2h"         → recurring every 2 hours
        "0 9 * * *"        → cron expression
        "2026-02-03T14:00" → once at timestamp
    """
    schedule = schedule.strip()
    original = schedule
    schedule_lower = schedule.lower()
    
    # "every X" pattern → recurring interval
    if schedule_lower.startswith("every "):
        duration_str = schedule[6:].strip()
        minutes = parse_duration(duration_str)
        return {
            "kind": "interval",
            "minutes": minutes,
            "display": f"every {minutes}m"
        }
    
    # Check for cron expression (5 or 6 space-separated fields)
    # Cron fields: minute hour day month weekday [year]
    parts = schedule.split()
    if len(parts) >= 5 and all(
        re.match(r'^[\d\*\-,/]+$', p) for p in parts[:5]
    ):
        if not HAS_CRONITER:
            raise ValueError("Cron expressions require 'croniter' package. Install with: pip install croniter")
        # Validate cron expression
        try:
            croniter(schedule)
        except Exception as e:
            raise ValueError(f"Invalid cron expression '{schedule}': {e}")
        return {
            "kind": "cron",
            "expr": schedule,
            "display": schedule
        }
    
    # ISO timestamp (contains T or looks like date)
    if 'T' in schedule or re.match(r'^\d{4}-\d{2}-\d{2}', schedule):
        try:
            # Parse and validate
            dt = datetime.fromisoformat(schedule.replace('Z', '+00:00'))
            # Make naive timestamps timezone-aware at parse time so the stored
            # value doesn't depend on the system timezone matching at check time.
            if dt.tzinfo is None:
                dt = dt.astimezone()  # Interpret as local timezone
            return {
                "kind": "once",
                "run_at": dt.isoformat(),
                "display": f"once at {dt.strftime('%Y-%m-%d %H:%M')}"
            }
        except ValueError as e:
            raise ValueError(f"Invalid timestamp '{schedule}': {e}")
    
    # Duration like "30m", "2h", "1d" → one-shot from now
    try:
        minutes = parse_duration(schedule)
        run_at = _hermes_now() + timedelta(minutes=minutes)
        return {
            "kind": "once",
            "run_at": run_at.isoformat(),
            "display": f"once in {original}"
        }
    except ValueError:
        pass
    
    raise ValueError(
        f"Invalid schedule '{original}'. Use:\n"
        f"  - Duration: '30m', '2h', '1d' (one-shot)\n"
        f"  - Interval: 'every 30m', 'every 2h' (recurring)\n"
        f"  - Cron: '0 9 * * *' (cron expression)\n"
        f"  - Timestamp: '2026-02-03T14:00:00' (one-shot at time)"
    )


def _ensure_aware(dt: datetime) -> datetime:
    """Return a timezone-aware datetime in Hermes configured timezone.

    Backward compatibility:
    - Older stored timestamps may be naive.
    - Naive values are interpreted as *system-local wall time* (the timezone
      `datetime.now()` used when they were created), then converted to the
      configured Hermes timezone.

    This preserves relative ordering for legacy naive timestamps across
    timezone changes and avoids false not-due results.
    """
    target_tz = _hermes_now().tzinfo
    if dt.tzinfo is None:
        local_tz = datetime.now().astimezone().tzinfo
        return dt.replace(tzinfo=local_tz).astimezone(target_tz)
    return dt.astimezone(target_tz)


def _timezone_offset_mismatch(stored: datetime, current: datetime) -> bool:
    """Return True when a stored aware timestamp uses a different UTC offset.

    Naive stored timestamps return False: they carry no offset to compare, and
    are normalized by ``_ensure_aware`` instead — they intentionally never take
    the offset-repair path.
    """
    if stored.tzinfo is None or current.tzinfo is None:
        return False
    return stored.utcoffset() != current.utcoffset()


def _stored_wall_clock_is_future(stored: datetime, current: datetime) -> bool:
    """Return True when the stored local wall-clock time has not arrived yet.

    Cron schedules express local wall-clock intent. If Hermes/system local time
    changes after next_run_at was persisted, an old offset can make a future
    wall-clock run look due at the converted absolute time. Comparing naive
    wall-clock values distinguishes that migration case from a missed run whose
    scheduled wall time has already passed.
    """
    return stored.replace(tzinfo=None) > current.replace(tzinfo=None)


def _recoverable_oneshot_run_at(
    schedule: Dict[str, Any],
    now: datetime,
    *,
    last_run_at: Optional[str] = None,
) -> Optional[str]:
    """Return a one-shot run time if it is still eligible to fire.

    One-shot jobs get a small grace window so jobs created a few seconds after
    their requested minute still run on the next tick. Once a one-shot has
    already run, it is never eligible again.
    """
    if not isinstance(schedule, dict) or schedule.get("kind") != "once":
        return None
    if last_run_at:
        return None

    run_at = schedule.get("run_at")
    if not run_at:
        return None

    try:
        run_at_dt = _ensure_aware(datetime.fromisoformat(run_at))
    except Exception:
        return None
    if run_at_dt >= now - timedelta(seconds=ONESHOT_GRACE_SECONDS):
        return run_at
    return None


def _compute_grace_seconds(schedule: dict) -> int:
    """Compute how late a job can be and still catch up instead of fast-forwarding.

    Uses half the schedule period, clamped between 120 seconds and 2 hours.
    This ensures daily jobs can catch up if missed by up to 2 hours,
    while frequent jobs (every 5-10 min) still fast-forward quickly.
    """
    MIN_GRACE = 120
    MAX_GRACE = 7200  # 2 hours

    kind = schedule.get("kind")

    if kind == "interval":
        period_seconds = schedule.get("minutes", 1) * 60
        grace = period_seconds // 2
        return max(MIN_GRACE, min(grace, MAX_GRACE))

    if kind == "cron" and HAS_CRONITER:
        expr = schedule.get("expr")
        if expr:
            try:
                now = _hermes_now()
                cron = croniter(expr, now)
                first = cron.get_next(datetime)
                second = cron.get_next(datetime)
                period_seconds = int((second - first).total_seconds())
                grace = period_seconds // 2
                return max(MIN_GRACE, min(grace, MAX_GRACE))
            except Exception:
                pass

    return MIN_GRACE


def _parse_cron_timestamp(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return _ensure_aware(datetime.fromisoformat(str(value)))
    except (TypeError, ValueError):
        return None


def _newest_job_output_path(job_id: str, output_root: Optional[Path] = None) -> Optional[str]:
    try:
        job_output_dir = (output_root or OUTPUT_DIR) / _job_output_dir(job_id).name
    except ValueError:
        return None
    try:
        candidates = [path for path in job_output_dir.glob("*.md") if path.is_file()]
    except OSError:
        return None
    if not candidates:
        return None
    newest = max(candidates, key=lambda path: (path.stat().st_mtime, path.name))
    return str(newest)


def _session_evidence_for_job(
    job_id: str,
    *,
    session_db_path: Optional[Path] = None,
    stale_after_seconds: int = 6 * 60 * 60,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    db_path = session_db_path or (get_hermes_home() / "state.db")
    evidence: Dict[str, Any] = {
        "session_id": None,
        "stale_open_session": False,
        "available": False,
        "unavailable_reason": None,
    }
    if not db_path.exists():
        evidence["unavailable_reason"] = f"session database not found: {db_path}"
        return evidence

    uri = f"file:{db_path}?mode=ro"
    prefix = f"cron_{job_id}_"
    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                """
                SELECT id, started_at, ended_at
                FROM sessions
                WHERE id LIKE ?
                ORDER BY started_at DESC
                LIMIT 1
                """,
                (f"{prefix}%",),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        evidence["unavailable_reason"] = f"session database unavailable: {exc}"
        return evidence

    evidence["available"] = True
    if not row:
        return evidence

    evidence["session_id"] = row["id"]
    if row["ended_at"] is None and row["started_at"] is not None:
        check_now = now or _hermes_now()
        try:
            age = check_now.timestamp() - float(row["started_at"])
            evidence["stale_open_session"] = age > stale_after_seconds
        except (TypeError, ValueError, OSError):
            evidence["stale_open_session"] = True
    return evidence


def _scheduler_log_excerpts(
    job_id: str,
    needles: List[str],
    *,
    log_root: Optional[Path] = None,
    max_lines: int = 5,
) -> Dict[str, Any]:
    root = log_root or (get_hermes_home() / "logs")
    evidence: Dict[str, Any] = {
        "excerpts": [],
        "available": False,
        "unavailable_reason": None,
    }
    if not root.exists():
        evidence["unavailable_reason"] = f"log directory not found: {root}"
        return evidence

    log_files: List[Path] = []
    for pattern in ("agent.log*", "gateway.log*", "errors.log*"):
        try:
            log_files.extend(path for path in root.glob(pattern) if path.is_file())
        except OSError:
            evidence["unavailable_reason"] = f"log directory unreadable: {root}"
            return evidence

    if not log_files:
        evidence["unavailable_reason"] = f"no scheduler logs found in: {root}"
        return evidence

    evidence["available"] = True
    terms = [term for term in [job_id, *needles] if term]
    excerpts: List[str] = []
    for log_file in sorted(log_files, key=lambda path: path.stat().st_mtime, reverse=True):
        try:
            lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in reversed(lines[-500:]):
            if any(term in line for term in terms):
                excerpts.append(f"{log_file.name}: {line[:500]}")
                if len(excerpts) >= max_lines:
                    evidence["excerpts"] = list(reversed(excerpts))
                    return evidence
    evidence["excerpts"] = list(reversed(excerpts))
    return evidence


def audit_overdue_self_improvement_proposals(
    *,
    jobs: Optional[List[Dict[str, Any]]] = None,
    now: Optional[datetime] = None,
    output_root: Optional[Path] = None,
    log_root: Optional[Path] = None,
    session_db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Return read-only overdue-run findings for proposal cron jobs.

    The detector mirrors ``get_due_jobs`` stale-run semantics: a recurring
    enabled proposal job is overdue only after its stored ``next_run_at`` is
    older than the scheduler grace window. It does not advance schedules or
    otherwise mutate cron state.
    """
    check_now = now or _hermes_now()
    source_jobs = jobs if jobs is not None else load_jobs()
    findings: List[Dict[str, Any]] = []

    for raw_job in source_jobs:
        job = _normalize_job_record(copy.deepcopy(raw_job))
        if not isinstance(job.get("self_improvement_proposal"), dict):
            continue
        if not job.get("enabled", True) or job.get("state") == "paused":
            continue
        if job.get("no_agent"):
            continue

        schedule = job.get("schedule") or {}
        if schedule.get("kind") not in {"cron", "interval"}:
            continue

        next_run_at = job.get("next_run_at")
        next_run_dt = _parse_cron_timestamp(next_run_at)
        if next_run_dt is None:
            continue

        grace_seconds = _compute_grace_seconds(schedule)
        overdue_after = next_run_dt + timedelta(seconds=grace_seconds)
        if check_now <= overdue_after:
            continue

        job_id = job.get("id", "unknown")
        session_evidence = _session_evidence_for_job(
            job_id,
            session_db_path=session_db_path,
            now=check_now,
        )
        needles = [
            str(next_run_at or ""),
            str(job.get("last_run_at") or ""),
            str(session_evidence.get("session_id") or ""),
            "missed its scheduled time",
            "Fast-forwarding to next run",
        ]
        log_evidence = _scheduler_log_excerpts(job_id, needles, log_root=log_root)

        findings.append(
            {
                "job_id": job_id,
                "job_name": job.get("name") or job_id,
                "expected_missed_slot": next_run_at,
                "next_run_at": next_run_at,
                "last_run_at": job.get("last_run_at"),
                "last_output_path": _newest_job_output_path(job_id, output_root),
                "grace_seconds": grace_seconds,
                "overdue_after": overdue_after.isoformat(),
                "session": session_evidence,
                "scheduler_logs": log_evidence,
            }
        )

    return findings


def detect_missed_cron_runs(
    *,
    jobs: Optional[List[Dict[str, Any]]] = None,
    now: Optional[datetime] = None,
    grace_seconds: int = 300,
    claim_ttl_seconds: int = 600,
    limit: int = 10,
) -> Dict[str, Any]:
    """Return read-only missed-run status for enabled cron jobs.

    This intentionally does not mirror ``get_due_jobs`` by advancing stale
    schedules. It reports persisted ``next_run_at`` values that are older than
    the operator grace window so scheduler-loop liveness can be assessed
    separately from missed scheduled fires.
    """
    check_now = now or _hermes_now()
    grace = max(0, int(grace_seconds or 0))
    claim_ttl = max(0, int(claim_ttl_seconds or 0))
    source_jobs = jobs if jobs is not None else load_jobs()
    offenders: List[Dict[str, Any]] = []

    for raw_job in source_jobs:
        job = _normalize_job_record(copy.deepcopy(raw_job))
        if not job.get("enabled", True) or job.get("state") in {"paused", "completed"}:
            continue

        schedule = job.get("schedule") if isinstance(job.get("schedule"), dict) else {}
        if schedule.get("kind") not in {"cron", "interval"}:
            continue

        next_run_at = job.get("next_run_at")
        next_run_dt = _parse_cron_timestamp(next_run_at)
        if next_run_dt is None:
            continue

        created_at_dt = _parse_cron_timestamp(job.get("created_at"))
        if created_at_dt and created_at_dt > next_run_dt:
            continue

        manual = job.get("manual_run") if isinstance(job.get("manual_run"), dict) else None
        if manual and manual.get("state") in {"queued", "running"}:
            continue

        claim = job.get("fire_claim") if isinstance(job.get("fire_claim"), dict) else None
        if claim:
            claimed_at = _parse_cron_timestamp(claim.get("claimed_at"))
            if claimed_at and (check_now - claimed_at).total_seconds() < claim_ttl:
                continue

        overdue_seconds = int((check_now - next_run_dt).total_seconds())
        if overdue_seconds <= grace:
            continue

        job_id = job.get("id", "unknown")
        offenders.append(
            {
                "job_id": job_id,
                "job_name": job.get("name") or job_id,
                "last_run_at": job.get("last_run_at"),
                "next_run_at": next_run_at,
                "overdue_seconds": overdue_seconds,
                "overdue_age": _format_duration_seconds(overdue_seconds),
                "last_status": job.get("last_status"),
                "last_output_path": job.get("last_terminal_output_path") or _newest_job_output_path(job_id),
            }
        )

    offenders.sort(key=lambda item: item["overdue_seconds"], reverse=True)
    limited = offenders[: max(0, int(limit or 0))]
    return {
        "missed_count": len(offenders),
        "grace_seconds": grace,
        "offenders": limited,
    }


def _format_duration_seconds(seconds: int) -> str:
    remaining = max(0, int(seconds))
    days, remaining = divmod(remaining, 86400)
    hours, remaining = divmod(remaining, 3600)
    minutes, secs = divmod(remaining, 60)
    parts: List[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)


def compute_next_run(schedule: Dict[str, Any], last_run_at: Optional[str] = None) -> Optional[str]:
    """
    Compute the next run time for a schedule.

    Returns ISO timestamp string, or None if no more runs.
    """
    now = _hermes_now()

    if not isinstance(schedule, dict):
        return None
    kind = schedule.get("kind")
    if kind is None:
        return None

    if kind == "once":
        return _recoverable_oneshot_run_at(schedule, now, last_run_at=last_run_at)

    elif kind == "interval":
        minutes = schedule.get("minutes")
        if minutes is None:
            return None
        if last_run_at:
            try:
                last = _ensure_aware(datetime.fromisoformat(last_run_at))
                next_run = last + timedelta(minutes=minutes)
            except Exception:
                next_run = now + timedelta(minutes=minutes)
        else:
            # First run is now + interval
            next_run = now + timedelta(minutes=minutes)
        return next_run.isoformat()

    elif kind == "cron":
        expr = schedule.get("expr")
        if not expr:
            return None
        if not HAS_CRONITER:
            logger.warning(
                "Cannot compute next run for cron schedule %r: 'croniter' is "
                "not installed. croniter is a core dependency as of v0.9.x; "
                "reinstall hermes-agent or run 'pip install croniter' in your "
                "runtime env.",
                expr,
            )
            return None
        # Use last_run_at as the croniter base when available, consistent
        # with interval jobs.  This ensures that after a crash/restart,
        # the next run is anchored to the actual last execution time
        # rather than to an arbitrary restart time.
        base_time = now
        if last_run_at:
            try:
                base_time = _ensure_aware(datetime.fromisoformat(last_run_at))
            except Exception:
                base_time = now
        cron = croniter(expr, base_time)
        next_run = cron.get_next(datetime)
        return next_run.isoformat()

    return None


# =============================================================================
# Job CRUD Operations
# =============================================================================

def load_jobs() -> List[Dict[str, Any]]:
    """Load all jobs from storage."""
    jobs_file = _current_cron_store().jobs_file
    ensure_dirs()
    if not jobs_file.exists():
        return []

    strict_retry = False
    try:
        # utf-8-sig: Windows Notepad / PowerShell 5.1 Set-Content -Encoding UTF8
        # write a leading BOM; json.load under plain utf-8 raises
        # JSONDecodeError("Unexpected UTF-8 BOM") and takes down cron.
        with open(jobs_file, 'r', encoding='utf-8-sig') as f:
            data = json.load(f)
    except json.JSONDecodeError:
        # Retry with strict=False to handle bare control chars in string values
        strict_retry = True
        try:
            with open(jobs_file, 'r', encoding='utf-8-sig') as f:
                data = json.loads(f.read(), strict=False)
        except Exception as e:
            logger.error("Failed to auto-repair jobs.json: %s", e)
            raise RuntimeError(f"Cron database corrupted and unrepairable: {e}") from e
    except IOError as e:
        logger.error("IOError reading jobs.json: %s", e)
        raise RuntimeError(f"Failed to read cron database: {e}") from e

    if isinstance(data, dict):
        jobs = data.get("jobs", [])
        if not isinstance(jobs, list):
            raise RuntimeError("Cron database corrupted: 'jobs' must be a list")
        if strict_retry and jobs:
            save_jobs(jobs)
            logger.warning("Auto-repaired jobs.json (had invalid control characters)")
        return jobs
    if isinstance(data, list):
        if data:
            save_jobs(data)
            logger.warning("Auto-repaired jobs.json (bare list wrapped as dict)")
        return data

    raise RuntimeError(
        f"Cron database corrupted: expected {{'jobs': [...]}}, got {type(data).__name__}"
    )


def _save_jobs_unlocked(jobs: List[Dict[str, Any]]):
    """Save all jobs to storage. Caller must hold _jobs_lock()."""
    jobs_file = _current_cron_store().jobs_file
    ensure_dirs()
    fd, tmp_path = tempfile.mkstemp(dir=str(jobs_file.parent), suffix='.tmp', prefix='.jobs_')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump({"jobs": jobs, "updated_at": _hermes_now().isoformat()}, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        atomic_replace(tmp_path, jobs_file)
        _secure_file(jobs_file)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def save_jobs(jobs: List[Dict[str, Any]]):
    """Save all jobs under the re-entrant cross-process store lock."""
    with _jobs_lock():
        _save_jobs_unlocked(jobs)


def _normalize_workdir(workdir: Optional[str]) -> Optional[str]:
    """Normalize and validate a cron job workdir.

    Rules:
      - Empty / None → None (feature off, preserves old behaviour).
      - ``~`` is expanded.  Relative paths are rejected — cron jobs run detached
        from any shell cwd, so relative paths have no stable meaning.
      - The path must exist and be a directory at create/update time.  We do
        NOT re-check at run time (a user might briefly unmount the dir; the
        scheduler will just fall back to old behaviour with a logged warning).

    Returns the absolute path string, or None when disabled.
    Raises ValueError on invalid input.
    """
    if workdir is None:
        return None
    raw = str(workdir).strip()
    if not raw:
        return None
    expanded = Path(raw).expanduser()
    if not expanded.is_absolute():
        raise ValueError(
            f"Cron workdir must be an absolute path (got {raw!r}). "
            f"Cron jobs run detached from any shell cwd, so relative paths are ambiguous."
        )
    resolved = expanded.resolve()
    if not resolved.exists():
        raise ValueError(f"Cron workdir does not exist: {resolved}")
    if not resolved.is_dir():
        raise ValueError(f"Cron workdir is not a directory: {resolved}")
    return str(resolved)


def _resolve_default_model_snapshot() -> Optional[str]:
    """Resolve the current unpinned default model for drift detection."""
    try:
        import yaml
        from hermes_cli.config import _expand_env_vars

        cfg_path = get_hermes_home() / "config.yaml"
        if not cfg_path.exists():
            return None
        with cfg_path.open(encoding="utf-8") as handle:
            cfg = yaml.safe_load(handle) or {}
        try:
            from hermes_cli import managed_scope

            cfg = managed_scope.apply_managed_overlay(cfg)
        except Exception:
            pass
        cfg = _expand_env_vars(cfg)
        model_cfg = cfg.get("model") or {}
        if isinstance(model_cfg, str):
            return model_cfg.strip() or None
        if isinstance(model_cfg, dict):
            value = model_cfg.get("default") or model_cfg.get("model")
            return value.strip() or None if isinstance(value, str) else None
    except Exception:
        pass
    return None


def _normalize_job_optional_text(
    value: Any, *, strip_trailing_slash: bool = False
) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if strip_trailing_slash:
        text = text.rstrip("/")
    return text or None


def _compute_provider_model_snapshots(
    *, provider: Any, model: Any, base_url: Any, no_agent: Any
) -> Tuple[Optional[str], Optional[str]]:
    """Snapshot unpinned inference axes so later global drift fails closed."""
    normalized_provider = _normalize_job_optional_text(provider)
    normalized_model = _normalize_job_optional_text(model)
    normalized_base_url = _normalize_job_optional_text(
        base_url, strip_trailing_slash=True
    )
    if bool(no_agent):
        return None, None
    provider_snapshot: Optional[str] = None
    model_snapshot: Optional[str] = None
    if normalized_provider is None:
        try:
            from hermes_cli.runtime_provider import resolve_runtime_provider

            kwargs: Dict[str, Any] = {"requested": None}
            if normalized_base_url:
                kwargs["explicit_base_url"] = normalized_base_url
            resolved = resolve_runtime_provider(**kwargs)
            provider_snapshot = str(resolved.get("provider") or "").strip().lower() or None
        except Exception:
            pass
    if normalized_model is None:
        model_snapshot = _resolve_default_model_snapshot()
    return provider_snapshot, model_snapshot


def _normalize_profile(profile: Optional[str]) -> Optional[str]:
    """Normalize and validate an optional cron job profile name.

    Empty / None disables per-job profile selection. Otherwise the profile name
    is canonicalized with the same rules as ``hermes -p`` and must refer to an
    existing profile at create/update time. ``default`` is the built-in root
    profile and is always valid.
    """
    if profile is None:
        return None
    raw = str(profile).strip()
    if not raw:
        return None

    from hermes_cli.profiles import normalize_profile_name, resolve_profile_env

    normalized = normalize_profile_name(raw)
    # resolve_profile_env validates the canonical name and checks that named
    # profiles exist. Store only the stable profile id, not the filesystem path,
    # so profile directories can move with the Hermes root.
    resolve_profile_env(normalized)
    return normalized


def create_job(
    prompt: Optional[str],
    schedule: str,
    name: Optional[str] = None,
    repeat: Optional[int] = None,
    deliver: Optional[str] = None,
    origin: Optional[Dict[str, Any]] = None,
    skill: Optional[str] = None,
    skills: Optional[List[str]] = None,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
    model_tier: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    script: Optional[str] = None,
    context_from: Optional[Union[str, List[str]]] = None,
    enabled_toolsets: Optional[List[str]] = None,
    workdir: Optional[str] = None,
    profile: Optional[str] = None,
    no_agent: bool = False,
    disable_on_terminal_success: bool = False,
) -> Dict[str, Any]:
    """
    Create a new cron job.

    Args:
        prompt: The prompt to run (must be self-contained, or a task instruction when skill is set).
                Ignored when ``no_agent=True`` except as an optional name hint.
        schedule: Schedule string (see parse_schedule)
        name: Optional friendly name
        repeat: How many times to run (None = forever, 1 = once)
        deliver: Where to deliver output ("origin", "local", "telegram", etc.)
        origin: Source info where job was created (for "origin" delivery)
        skill: Optional legacy single skill name to load before running the prompt
        skills: Optional ordered list of skills to load before running the prompt
        model_tier: Optional named model tier used when raw runtime overrides are absent
        model: Optional per-job model override
        provider: Optional per-job provider override
        reasoning_effort: Optional per-job reasoning override
        base_url: Optional per-job base URL override
        script: Optional path to a script whose stdout feeds the job. With
                ``no_agent=True`` the script IS the job — its stdout is
                delivered verbatim. Without ``no_agent``, its stdout is
                injected into the agent's prompt as context (data-collection /
                change-detection pattern). Paths resolve under
                ~/.hermes/scripts/; ``.sh`` / ``.bash`` files run via bash,
                anything else via Python.
        context_from: Optional job ID (or list of job IDs) whose most recent output
                      is injected into the prompt as context before each run.
                      Useful for chaining cron jobs: job A finds data, job B processes it.
        enabled_toolsets: Optional list of toolset names to restrict the agent to.
                          When set, only tools from these toolsets are loaded, reducing
                          token overhead. When omitted, all default tools are loaded.
                          Ignored when ``no_agent=True``.
        workdir: Optional absolute path.  When set, the job runs as if launched
                from that directory: AGENTS.md / CLAUDE.md / .cursorrules from
                that directory are injected into the system prompt, and the
                terminal/file/code_exec tools use it as their working directory
                (via TERMINAL_CWD).  When unset, the old behaviour is preserved
                (no context files injected, tools use the scheduler's cwd).
                With ``no_agent=True``, ``workdir`` is still applied as the
                script's cwd so relative paths inside the script behave
                predictably.
        profile: Optional Hermes profile name. When set, the job runs with
                that profile's HERMES_HOME so profile-specific config,
                credentials, scripts, skills, and memory paths resolve
                consistently. ``default`` selects the root profile; empty /
                None preserves the scheduler's existing behaviour.
        no_agent: When True, skip the agent entirely — run ``script`` on schedule
                and deliver its stdout directly. Empty stdout = silent (no
                delivery). Requires ``script`` to be set. Ideal for classic
                watchdogs and periodic alerts that don't need LLM reasoning.

    Returns:
        The created job dict
    """
    from cron.lifecycle_guard import check_gateway_lifecycle

    parsed_schedule = parse_schedule(schedule)

    # Normalize repeat: treat 0 or negative values as None (infinite)
    if repeat is not None and repeat <= 0:
        repeat = None

    # Auto-set repeat=1 for one-shot schedules if not specified
    if parsed_schedule["kind"] == "once" and repeat is None:
        repeat = 1

    # Default delivery to origin if available, otherwise local
    if deliver is None:
        deliver = "origin" if origin else "local"

    job_id = uuid.uuid4().hex[:12]
    now = _hermes_now().isoformat()

    normalized_skills = _normalize_skill_list(skill, skills)
    normalized_model_tier = str(model_tier).strip() if isinstance(model_tier, str) else None
    normalized_model = str(model).strip() if isinstance(model, str) else None
    normalized_provider = str(provider).strip() if isinstance(provider, str) else None
    normalized_reasoning_effort = str(reasoning_effort).strip() if isinstance(reasoning_effort, str) else None
    normalized_base_url = str(base_url).strip().rstrip("/") if isinstance(base_url, str) else None
    normalized_model_tier = normalized_model_tier or None
    normalized_model = normalized_model or None
    normalized_provider = normalized_provider or None
    if normalized_reasoning_effort:
        from hermes_constants import VALID_REASONING_EFFORTS, normalize_reasoning_effort

        normalized_reasoning_effort = normalize_reasoning_effort(normalized_reasoning_effort)
        if normalized_reasoning_effort not in {"none", *VALID_REASONING_EFFORTS}:
            raise ValueError(f"Unknown reasoning effort: {reasoning_effort}")
    normalized_reasoning_effort = normalized_reasoning_effort or None
    normalized_base_url = normalized_base_url or None
    normalized_script = str(script).strip() if isinstance(script, str) else None
    normalized_script = normalized_script or None
    check_gateway_lifecycle(prompt, normalized_script)
    normalized_toolsets = [str(t).strip() for t in enabled_toolsets if str(t).strip()] if enabled_toolsets else None
    normalized_toolsets = normalized_toolsets or None
    normalized_workdir = _normalize_workdir(workdir)
    normalized_profile = _normalize_profile(profile)
    normalized_no_agent = bool(no_agent)

    # no_agent jobs are meaningless without a script — the script IS the job.
    # Surface this as a clear ValueError at create time so bad configs never
    # reach the scheduler.
    if normalized_no_agent and not normalized_script:
        raise ValueError(
            "no_agent=True requires a script — with no agent and no script "
            "there is nothing for the job to run."
        )

    # Normalize context_from: accept str or list of str, store as list or None
    if isinstance(context_from, str):
        context_from = [context_from.strip()] if context_from.strip() else None
    elif isinstance(context_from, list):
        context_from = [str(j).strip() for j in context_from if str(j).strip()] or None
    else:
        context_from = None

    prompt_text = _coerce_job_text(prompt)
    label_source = (prompt_text or (normalized_skills[0] if normalized_skills else None) or (normalized_script if normalized_no_agent else None)) or "cron job"

    provider_snapshot, model_snapshot = _compute_provider_model_snapshots(
        provider=normalized_provider,
        model=normalized_model,
        base_url=normalized_base_url,
        no_agent=normalized_no_agent,
    )

    next_run_at = compute_next_run(parsed_schedule)
    if parsed_schedule.get("kind") == "once" and next_run_at is None:
        run_at = parsed_schedule.get("run_at") or schedule
        logger.warning(
            "Rejecting one-shot cron job '%s': run_at %s is outside the %ss grace window",
            name or label_source[:50].strip(),
            run_at,
            ONESHOT_GRACE_SECONDS,
        )
        raise ValueError(
            f"Requested one-shot time {run_at} is more than "
            f"{ONESHOT_GRACE_SECONDS}s in the past and cannot be scheduled."
        )
    job = {
        "id": job_id,
        "name": name or label_source[:50].strip(),
        "prompt": prompt_text,
        "skills": normalized_skills,
        "skill": normalized_skills[0] if normalized_skills else None,
        "model_tier": normalized_model_tier,
        "model": normalized_model,
        "provider": normalized_provider,
        "reasoning_effort": normalized_reasoning_effort,
        "base_url": normalized_base_url,
        "script": normalized_script,
        "no_agent": normalized_no_agent,
        "disable_on_terminal_success": bool(disable_on_terminal_success),
        "context_from": context_from,
        "schedule": parsed_schedule,
        "schedule_display": parsed_schedule.get("display", schedule),
        "repeat": {
            "times": repeat,  # None = forever
            "completed": 0
        },
        "enabled": True,
        "state": "scheduled",
        "paused_at": None,
        "paused_reason": None,
        "created_at": now,
        "next_run_at": next_run_at,
        "last_run_at": None,
        "last_status": None,
        "last_error": None,
        "last_delivery_error": None,
        # Delivery configuration
        "deliver": deliver,
        "origin": origin,  # Tracks where job was created for "origin" delivery
        "enabled_toolsets": normalized_toolsets,
        "workdir": normalized_workdir,
        "profile": normalized_profile,
        "provider_snapshot": provider_snapshot,
        "model_snapshot": model_snapshot,
    }

    jobs = load_jobs()
    jobs.append(job)
    save_jobs(jobs)

    return job


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    """Get a job by ID."""
    jobs = load_jobs()
    for job in jobs:
        if job["id"] == job_id:
            return _normalize_job_record(job)
    return None


class AmbiguousJobReference(LookupError):
    """Raised when a job name matches more than one job."""

    def __init__(self, ref: str, matches: List[Dict[str, Any]]):
        self.ref = ref
        self.matches = matches
        ids = ", ".join(m["id"] for m in matches)
        super().__init__(
            f"Job name '{ref}' is ambiguous — matches {len(matches)} jobs: {ids}. "
            f"Use the job ID instead."
        )


def resolve_job_ref(ref: str) -> Optional[Dict[str, Any]]:
    """Resolve a job reference (ID or name) to a job record.

    - Exact ID match wins (works even if a different job's name equals this ID).
    - Otherwise, case-insensitive name match.
    - If a name matches more than one job, raises AmbiguousJobReference so the
      caller can surface the matching IDs rather than silently picking one.
    """
    if not ref:
        return None
    jobs = load_jobs()
    for job in jobs:
        if job["id"] == ref:
            return _normalize_job_record(job)
    ref_lower = ref.lower()
    name_matches = [j for j in jobs if (j.get("name") or "").lower() == ref_lower]
    if not name_matches:
        return None
    if len(name_matches) > 1:
        raise AmbiguousJobReference(
            ref, [_normalize_job_record(j) for j in name_matches]
        )
    return _normalize_job_record(name_matches[0])


def list_jobs(include_disabled: bool = False) -> List[Dict[str, Any]]:
    """List all jobs, optionally including disabled ones."""
    jobs = [_normalize_job_record(j) for j in load_jobs()]
    if not include_disabled:
        jobs = [j for j in jobs if j.get("enabled", True)]
    try:
        from cron.executions import latest_executions

        latest = latest_executions([job.get("id", "") for job in jobs])
    except Exception:
        latest = {}
    for job in jobs:
        job["latest_execution"] = latest.get(job.get("id", ""))
    return jobs


def update_job(job_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Update a job by ID, refreshing derived schedule fields when needed."""
    # Block mutation of immutable fields. ``id`` in particular is a filesystem
    # path component under OUTPUT_DIR — letting an update change it leaks
    # path-escape values into output writes/deletes.
    bad_fields = _IMMUTABLE_JOB_FIELDS.intersection(updates or {})
    if bad_fields:
        raise ValueError(
            f"Cron job field(s) cannot be updated: {', '.join(sorted(bad_fields))}"
        )

    with _jobs_lock():
        jobs = load_jobs()
        for i, job in enumerate(jobs):
            if job["id"] != job_id:
                continue
            if "workdir" in updates:
                value = updates["workdir"]
                updates["workdir"] = None if value in {None, "", False} else _normalize_workdir(value)
            if "profile" in updates:
                value = updates["profile"]
                updates["profile"] = None if value in {None, "", False} else _normalize_profile(value)
            for field in ("model_tier", "model", "provider", "reasoning_effort", "base_url"):
                if field in updates:
                    updates[field] = str(updates[field] or "").strip() or None
            if updates.get("reasoning_effort"):
                from hermes_constants import VALID_REASONING_EFFORTS, normalize_reasoning_effort

                updates["reasoning_effort"] = normalize_reasoning_effort(updates["reasoning_effort"])
                if updates["reasoning_effort"] not in {"none", *VALID_REASONING_EFFORTS}:
                    raise ValueError(f"Unknown reasoning effort: {updates['reasoning_effort']}")

            previous_axes = (
                _normalize_job_optional_text(job.get("provider")),
                _normalize_job_optional_text(job.get("model")),
                _normalize_job_optional_text(job.get("base_url"), strip_trailing_slash=True),
                bool(job.get("no_agent")),
            )
            updated = _apply_skill_fields({**job, **updates})
            if "skills" in updates or "skill" in updates:
                normalized_skills = _normalize_skill_list(updated.get("skill"), updated.get("skills"))
                updated["skills"] = normalized_skills
                updated["skill"] = normalized_skills[0] if normalized_skills else None

            if "schedule" in updates:
                schedule = updated["schedule"]
                if isinstance(schedule, str):
                    schedule = parse_schedule(schedule)
                    updated["schedule"] = schedule
                updated["schedule_display"] = updates.get(
                    "schedule_display", schedule.get("display", updated.get("schedule_display"))
                )
                if updated.get("state") != "paused":
                    next_run = compute_next_run(schedule)
                    if next_run is None and schedule.get("kind") == "once":
                        run_at = schedule.get("run_at") or schedule
                        raise ValueError(
                            f"Requested one-shot time {run_at} is more than "
                            f"{ONESHOT_GRACE_SECONDS}s in the past and cannot be scheduled."
                        )
                    updated["next_run_at"] = next_run

            new_axes = (
                _normalize_job_optional_text(updated.get("provider")),
                _normalize_job_optional_text(updated.get("model")),
                _normalize_job_optional_text(updated.get("base_url"), strip_trailing_slash=True),
                bool(updated.get("no_agent")),
            )
            if new_axes != previous_axes:
                provider_snapshot, model_snapshot = _compute_provider_model_snapshots(
                    provider=updated.get("provider"),
                    model=updated.get("model"),
                    base_url=updated.get("base_url"),
                    no_agent=updated.get("no_agent"),
                )
                updated["provider_snapshot"] = provider_snapshot
                updated["model_snapshot"] = model_snapshot

            if updated.get("enabled", True) and updated.get("state") != "paused" and not updated.get("next_run_at"):
                next_run = compute_next_run(updated["schedule"])
                if next_run is None and updated["schedule"].get("kind") == "once":
                    run_at = updated["schedule"].get("run_at", "unknown")
                    raise ValueError(
                        f"Requested one-shot time {run_at} is in the past "
                        f"(grace window: {ONESHOT_GRACE_SECONDS}s) and cannot be scheduled."
                    )
                updated["next_run_at"] = next_run

            jobs[i] = updated
            _save_jobs_unlocked(jobs)
            return _normalize_job_record(updated)
    return None


def pause_job(job_id: str, reason: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Pause a job without deleting it. Accepts a job ID or name."""
    job = resolve_job_ref(job_id)
    if not job:
        return None
    return update_job(
        job["id"],
        {
            "enabled": False,
            "state": "paused",
            "paused_at": _hermes_now().isoformat(),
            "paused_reason": reason,
        },
    )


def resume_job(job_id: str) -> Optional[Dict[str, Any]]:
    """Resume a paused job and compute the next future run from now. Accepts a job ID or name."""
    job = resolve_job_ref(job_id)
    if not job:
        return None

    next_run_at = compute_next_run(job["schedule"])
    if next_run_at is None and job["schedule"].get("kind") == "once":
        run_at = job["schedule"].get("run_at", "unknown")
        raise ValueError(
            f"Cannot resume: one-shot time {run_at} is in the past "
            f"(grace window: {ONESHOT_GRACE_SECONDS}s) and will never fire."
        )
    return update_job(
        job["id"],
        {
            "enabled": True,
            "state": "scheduled",
            "paused_at": None,
            "paused_reason": None,
            "next_run_at": next_run_at,
        },
    )


def trigger_job(job_id: str) -> Optional[Dict[str, Any]]:
    """Schedule a job to run on the next scheduler tick. Accepts a job ID or name."""
    job = resolve_job_ref(job_id)
    if not job:
        return None
    run_id = uuid.uuid4().hex[:12]
    now = _hermes_now().isoformat()
    return update_job(
        job["id"],
        {
            "enabled": True,
            "state": "scheduled",
            "paused_at": None,
            "paused_reason": None,
            "next_run_at": now,
            "manual_run": {
                "run_id": run_id,
                "requested_at": now,
                "state": "queued",
                "started_at": None,
                "pid": None,
                "output_path": None,
                "error": None,
            },
        },
    )


def mark_manual_run_started(
    job_id: str,
    run_id: str,
    pid: int,
    *,
    output_path: Optional[str] = None,
) -> None:
    """Persist that a manually-triggered cron run is actively executing."""
    with _jobs_lock():
        jobs = load_jobs()
        for job in jobs:
            if job.get("id") != job_id:
                continue
            manual = job.get("manual_run") or {}
            if manual.get("run_id") != run_id:
                return
            manual.update(
                {
                    "state": "running",
                    "started_at": manual.get("started_at") or _hermes_now().isoformat(),
                    "pid": pid,
                    "output_path": str(output_path) if output_path else manual.get("output_path"),
                    "error": None,
                }
            )
            job["manual_run"] = manual
            save_jobs(jobs)
            return


def mark_manual_run_finished(
    job_id: str,
    run_id: str,
    *,
    success: bool,
    output_path: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    """Persist the terminal status for the current manual cron run, if any."""
    with _jobs_file_lock:
        jobs = load_jobs()
        for job in jobs:
            if job.get("id") != job_id:
                continue
            manual = job.get("manual_run") or {}
            if manual.get("run_id") != run_id:
                return
            manual.update(
                {
                    "state": "completed" if success else "error",
                    "finished_at": _hermes_now().isoformat(),
                    "output_path": str(output_path) if output_path else manual.get("output_path"),
                    "error": error,
                    "pid": None,
                }
            )
            job["manual_run"] = manual
            save_jobs(jobs)
            return


def reconcile_manual_runs(pid_exists=None) -> int:
    """Repair in-flight manual run records after a scheduler/gateway restart."""
    if pid_exists is None:
        pid_exists = _pid_exists
    repaired = 0
    with _jobs_file_lock:
        jobs = load_jobs()
        for job in jobs:
            manual = job.get("manual_run")
            if not isinstance(manual, dict):
                continue
            state = manual.get("state")
            if state not in {"queued", "running"}:
                continue
            pid = manual.get("pid")
            if state == "running" and pid:
                try:
                    if pid_exists(int(pid)):
                        continue
                except (TypeError, ValueError, OSError):
                    pass
            manual.update(
                {
                    "state": "interrupted",
                    "finished_at": _hermes_now().isoformat(),
                    "error": "Manual cron run was interrupted before completion or lost during restart.",
                    "pid": None,
                }
            )
            job["manual_run"] = manual
            repaired += 1
        if repaired:
            save_jobs(jobs)
    return repaired


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def remove_job(job_id: str) -> bool:
    """Remove a job by ID or name."""
    job = resolve_job_ref(job_id)
    if not job:
        return False
    canonical_id = job["id"]
    jobs = load_jobs()
    original_len = len(jobs)
    jobs = [j for j in jobs if j["id"] != canonical_id]
    if len(jobs) < original_len:
        # Resolve the output dir BEFORE saving so a legacy unsafe ID (e.g.
        # left over from before the create-time guard) fails closed without
        # half-applying the removal.
        job_output_dir = _job_output_dir(canonical_id)
        save_jobs(jobs)
        # Clean up output directory to prevent orphaned dirs accumulating
        if job_output_dir.exists():
            shutil.rmtree(job_output_dir)
        return True
    return False


def mark_job_run(job_id: str, success: bool, error: Optional[str] = None,
                 delivery_error: Optional[str] = None,
                 health_details: Optional[Dict[str, Any]] = None):
    """
    Mark a job as having been run.
    
    Updates last_run_at, last_status, increments completed count,
    computes next_run_at, and auto-deletes if repeat limit reached.

    ``delivery_error`` is tracked separately from the agent error — a job
    can succeed (agent produced output) but fail delivery (platform down).
    """
    with _jobs_lock():
        jobs = load_jobs()
        for i, job in enumerate(jobs):
            if job["id"] == job_id:
                now = _hermes_now().isoformat()
                job["last_run_at"] = now
                job["last_status"] = "ok" if success else "error"
                job["last_error"] = error if not success else None
                job["fire_claim"] = None
                # Track delivery failures separately — cleared on successful delivery
                job["last_delivery_error"] = delivery_error
                job["last_health_details"] = copy.deepcopy(health_details) if health_details else None
                # Clear any external-fire claim so a re-armed recurring job can
                # be claimed again on its next fire (Phase 4C CAS).
                job["fire_claim"] = None
                # Clear the one-shot running-claim (#59229): the run is over, so
                # a re-armed recurring job or a re-dispatched one-shot recovery
                # is claimable again. No-op if the job never carried a claim.
                if job.get("run_claim") is not None:
                    job["run_claim"] = None
                
                # Increment completed count
                if job.get("repeat"):
                    repeat = job["repeat"]
                    times = repeat.get("times")
                    completed = repeat.get("completed", 0)
                    preclaimed_oneshot = (
                        job.get("schedule", {}).get("kind") == "once"
                        and times is not None
                        and times > 0
                        and completed > 0
                    )
                    if not preclaimed_oneshot:
                        completed += 1
                        repeat["completed"] = completed
                    
                    # Check if we've hit the repeat limit
                    if times is not None and times > 0 and completed >= times:
                        # Remove the job (limit reached)
                        jobs.pop(i)
                        save_jobs(jobs)
                        return
                
                # Compute next run
                job["next_run_at"] = compute_next_run(job["schedule"], now)

                # If no next run, decide whether this is terminal completion
                # (one-shot) or a transient failure (recurring schedule couldn't
                # compute — e.g. 'croniter' missing from the runtime env).
                # Recurring jobs must NEVER be silently disabled: that turns a
                # missing runtime dep into "job completed" and the user's
                # schedule quietly goes off. See issue #16265.
                if job["next_run_at"] is None:
                    kind = job.get("schedule", {}).get("kind")
                    if kind in {"cron", "interval"}:
                        job["state"] = "error"
                        if not job.get("last_error"):
                            job["last_error"] = (
                                "Failed to compute next run for recurring "
                                "schedule (is the 'croniter' package "
                                "installed in the gateway's Python env?)"
                            )
                        logger.error(
                            "Job '%s' (%s) could not compute next_run_at; "
                            "leaving enabled and marking state=error so the "
                            "job is not silently disabled.",
                            job.get("name", job.get("id", "?")),
                            kind,
                        )
                    else:
                        job["enabled"] = False
                        job["state"] = "completed"
                elif job.get("state") != "paused":
                    job["state"] = "scheduled"

                save_jobs(jobs)
                return

        logger.warning("mark_job_run: job_id %s not found, skipping save", job_id)


def claim_dispatch(job_id: str) -> bool:
    """Atomically claim a finite one-shot dispatch before its side effect."""
    with _jobs_lock():
        jobs = load_jobs()
        for i, job in enumerate(jobs):
            if job.get("id") != job_id:
                continue
            if job.get("schedule", {}).get("kind") != "once":
                return True
            repeat = job.get("repeat")
            if not repeat:
                return True
            times = repeat.get("times")
            if times is None or times <= 0:
                return True
            completed = repeat.get("completed", 0)
            if completed >= times:
                jobs.pop(i)
                _save_jobs_unlocked(jobs)
                return False
            repeat["completed"] = completed + 1
            _save_jobs_unlocked(jobs)
            return True
    return True


def mark_job_terminal_success(
    job_id: str,
    *,
    output_path: str,
    reason: str = "terminal success: DONE marker",
) -> Optional[Dict[str, Any]]:
    """Pause an opt-in finite no-agent job after terminal-success output.

    Called after the run output has been persisted and after ``mark_job_run``
    records last-run status/history, so pausing cannot lose run evidence.
    """
    with _jobs_lock():
        jobs = load_jobs()
        for i, job in enumerate(jobs):
            if job.get("id") != job_id:
                continue
            job.update(
                {
                    "enabled": False,
                    "state": "paused",
                    "paused_at": _hermes_now().isoformat(),
                    "paused_reason": reason,
                    "next_run_at": None,
                    "last_terminal_output_path": str(output_path),
                }
            )
            jobs[i] = job
            save_jobs(jobs)
            return _normalize_job_record(job)
    logger.warning("mark_job_terminal_success: job_id %s not found, skipping save", job_id)
    return None


def heartbeat_run_claim(job_id: str, *, expected_owner: str) -> bool:
    """Refresh a one-shot's ``run_claim`` timestamp while its run is alive.

    Called periodically from the scheduler's run monitor (#62002) so a
    legitimately long run keeps its claim fresh: an expired claim then really
    does mean "the claiming process died", and neither another process's tick
    nor this process's own next tick will re-dispatch or stale-remove the job
    while the run is in flight. mark_job_run() clears the claim on completion.

    ``expected_owner`` is the stable owner copied from the dispatched job. The
    compare-and-refresh prevents a stale runner that resumes after a long sleep
    from extending a claim another scheduler process has since taken over.

    Returns True if this owner's one-shot claim was refreshed; False when the
    job, claim, or ownership no longer matches.
    """
    with _jobs_lock():
        jobs = load_jobs()
        for job in jobs:
            if job.get("id") != job_id:
                continue
            if job.get("schedule", {}).get("kind") != "once":
                return False
            claim = job.get("run_claim")
            if not isinstance(claim, dict) or claim.get("by") != expected_owner:
                return False
            claim["at"] = _hermes_now().isoformat()
            save_jobs(jobs)
            return True
    return False


def advance_next_run(job_id: str) -> bool:
    """Preemptively advance next_run_at for a recurring job before execution.

    Call this BEFORE run_job() so that if the process crashes mid-execution,
    the job won't re-fire on the next gateway restart.  This converts the
    scheduler from at-least-once to at-most-once for recurring jobs — missing
    one run is far better than firing dozens of times in a crash loop.

    One-shot jobs are left unchanged so they can still retry on restart.

    Returns True if next_run_at was advanced, False otherwise.
    """
    with _jobs_file_lock:
        jobs = load_jobs()
        for job in jobs:
            if job["id"] == job_id:
                kind = job.get("schedule", {}).get("kind")
                if kind not in {"cron", "interval"}:
                    return False
                now = _hermes_now().isoformat()
                new_next = compute_next_run(job["schedule"], now)
                if new_next and new_next != job.get("next_run_at"):
                    job["next_run_at"] = new_next
                    save_jobs(jobs)
                    return True
                return False
        return False


def _machine_id() -> str:
    """Return a stable-ish identifier for claim attribution and debugging."""
    explicit = os.getenv("HERMES_MACHINE_ID", "").strip()
    if explicit:
        return explicit
    try:
        import socket

        host = socket.gethostname()
    except Exception:
        host = "unknown"
    return f"{host}:{os.getpid()}"


def claim_job_for_fire(
    job_id: str,
    claim_ttl_seconds: int = 600,
    *,
    expected_fire_at: str | None = None,
) -> bool:
    """Atomically claim a due cron job fire.

    External schedulers and immediate/manual runs call this before executing a
    job outside the built-in ticker. The claim is a storage-level compare and
    set: only the requested ``next_run_at`` occurrence can be claimed when
    ``expected_fire_at`` is supplied. A successful claim advances/clears
    ``next_run_at`` before execution so a concurrent retry or gateway tick
    cannot run the same fire twice.

    Returns ``True`` when this caller owns the fire, otherwise ``False``.
    """
    with _jobs_file_lock:
        jobs = load_jobs()
        now_dt = _hermes_now()
        now_iso = now_dt.isoformat()

        for job in jobs:
            if job.get("id") != job_id:
                continue
            if not job.get("enabled", True) or job.get("state") == "paused":
                return False
            claim = job.get("fire_claim") if isinstance(job.get("fire_claim"), dict) else None
            if claim:
                claimed_at = _parse_cron_timestamp(
                    claim.get("claimed_at") or claim.get("at")
                )
                if claimed_at:
                    age = (now_dt - claimed_at).total_seconds()
                    if 0 <= age < claim_ttl_seconds:
                        return False

            manual = job.get("manual_run") if isinstance(job.get("manual_run"), dict) else None
            if manual and manual.get("state") == "running":
                return False

            next_run = job.get("next_run_at")
            if not next_run:
                return False

            try:
                next_run_dt = _ensure_aware(datetime.fromisoformat(next_run))
            except (TypeError, ValueError):
                logger.error("claim_job_for_fire: invalid next_run_at for job %s: %r", job_id, next_run)
                return False

            if expected_fire_at is not None:
                expected_dt = _parse_cron_timestamp(expected_fire_at)
                if expected_dt is None or expected_dt != next_run_dt:
                    return False

            schedule = job.get("schedule", {}) if isinstance(job.get("schedule"), dict) else {}
            kind = schedule.get("kind")
            if kind in {"cron", "interval"}:
                grace = _compute_grace_seconds(schedule)
                if (now_dt - next_run_dt).total_seconds() > grace:
                    new_next = compute_next_run(schedule, now_iso)
                    if new_next:
                        job["next_run_at"] = new_next
                        save_jobs(jobs)
                    return False

                new_next = compute_next_run(schedule, now_iso)
                if not new_next:
                    return False
                job["next_run_at"] = new_next
                if job.get("state") != "paused":
                    job["state"] = "scheduled"
            else:
                # One-shot/manual fire: clear the due timestamp so a retry does
                # not also win before mark_job_run disables/completes the job.
                job["next_run_at"] = None
                if job.get("state") != "paused":
                    job["state"] = "running"

            job["fire_claim"] = {
                "claimed_at": now_iso,
                "claimed_for": next_run,
                "token": uuid.uuid4().hex,
            }
            save_jobs(jobs)
            return True

    return False


def get_due_jobs() -> List[Dict[str, Any]]:
    """Get all jobs that are due to run now.

    For recurring jobs (cron/interval), if the scheduled time is stale
    (more than one period in the past, e.g. because the gateway was down),
    the job is fast-forwarded to the next future run instead of firing
    immediately.  This prevents a burst of missed jobs on gateway restart.
    """
    with _jobs_file_lock:
        return _get_due_jobs_locked()


def _get_due_jobs_locked() -> List[Dict[str, Any]]:
    """Inner implementation of get_due_jobs(); must be called with _jobs_file_lock held."""
    now = _hermes_now()
    raw_jobs = load_jobs()
    needs_save = False

    # Repair id-less records BEFORE anything keys off ``job["id"]``. A direct
    # jobs.json edit that bypassed add_job() can leave a record without an "id"
    # (older writers used "job_id"). Every downstream site — the logging
    # helpers and the ``for rj in raw_jobs: if rj["id"] == job["id"]``
    # persistence loops — indexes job["id"] eagerly, so a single malformed
    # record raised KeyError mid-tick, aborting the whole scan before
    # save_jobs() ran. That froze the entire profile's scheduler in a
    # per-minute fast-forward loop (healthy jobs recomputed in memory, then
    # discarded when the exception unwound). Recover the id from the drifted
    # "job_id" key when present, else synthesize one, and persist.
    for rj in raw_jobs:
        if not rj.get("id"):
            rj["id"] = rj.pop("job_id", None) or uuid.uuid4().hex[:12]
            needs_save = True

    jobs = [_apply_skill_fields(j) for j in copy.deepcopy(raw_jobs)]
    due = []

    # Normalize malformed "schedule" records (direct jobs.json edit, old writers,
    # corruption, etc.). "schedule" must be a dict; a null/string/etc. value
    # makes `schedule.get("kind")` or direct `schedule["kind"]` / ["expr"] /
    # ["minutes"] later raise and abort the entire scan *before* save_jobs().
    # Healthy jobs then lose their fast-forwarded next_run_at (exactly the
    # failure mode of the id-less job bug fixed above). Repair early at the
    # source so the rest of the tick can proceed and persist progress for
    # siblings.
    for j in jobs:
        if not isinstance(j.get("schedule"), dict):
            j["schedule"] = {}
            needs_save = True
    for rj in raw_jobs:
        if not isinstance(rj.get("schedule"), dict):
            rj["schedule"] = {}
            needs_save = True

    # Normalize malformed "next_run_at" records (direct jobs.json edit,
    # corruption, migration, or buggy writer). If present but not a valid
    # ISO string, datetime.fromisoformat(next_run) later raises and aborts
    # the entire scan *before* save_jobs(). Healthy siblings then lose any
    # fast-forwarded next_run_at (same class of bug as bad "id" or "schedule").
    # Strip the bad value so the existing "no next_run_at" recovery path
    # recomputes a sane value and persists it for this job.
    for j in jobs:
        nr = j.get("next_run_at")
        if nr is not None:
            if not isinstance(nr, str):
                j.pop("next_run_at", None)
                needs_save = True
            else:
                try:
                    datetime.fromisoformat(nr)
                except Exception:
                    j.pop("next_run_at", None)
                    needs_save = True
    for rj in raw_jobs:
        nr = rj.get("next_run_at")
        if nr is not None:
            if not isinstance(nr, str):
                rj.pop("next_run_at", None)
                needs_save = True
            else:
                try:
                    datetime.fromisoformat(nr)
                except Exception:
                    rj.pop("next_run_at", None)
                    needs_save = True

    # Same treatment for last_run_at (used as base in recovery / compute_next_run).
    for j in jobs:
        lr = j.get("last_run_at")
        if lr is not None and not isinstance(lr, str):
            j.pop("last_run_at", None)
            needs_save = True
        elif isinstance(lr, str):
            try:
                datetime.fromisoformat(lr)
            except Exception:
                j.pop("last_run_at", None)
                needs_save = True
    for rj in raw_jobs:
        lr = rj.get("last_run_at")
        if lr is not None and not isinstance(lr, str):
            rj.pop("last_run_at", None)
            needs_save = True
        elif isinstance(lr, str):
            try:
                datetime.fromisoformat(lr)
            except Exception:
                rj.pop("last_run_at", None)
                needs_save = True

    # Resolve the one-shot running-claim stale-recovery TTL once per scan
    # (derived from HERMES_CRON_TIMEOUT). See _oneshot_run_claim_ttl_seconds.
    _run_claim_ttl = _oneshot_run_claim_ttl_seconds()

    for job in jobs:
        # Per-job containment (structural guard): one malformed or
        # unexpected job record must never abort the whole scan. The id /
        # schedule / timestamp normalizations above repair the known shapes;
        # this guard catches every FUTURE variant, degrading to "skip this
        # job this tick" so healthy siblings still run and their recovered
        # state still reaches save_jobs() below.
        try:
            if not job.get("enabled", True):
                continue

            # Cross-process running-claim guard (#59229): if another scheduler
            # process already claimed this one-shot and its run is still in flight
            # (claim younger than the TTL), skip it — do NOT re-dispatch. The
            # claim is stamped just before we return the job as due (below) and
            # cleared by mark_job_run() on completion. A claim older than the TTL
            # is treated as stale (the claiming tick died mid-run) and allowed
            # through so the job is recovered rather than wedged forever.
            existing_claim = job.get("run_claim")
            if existing_claim and job.get("schedule", {}).get("kind") == "once":
                try:
                    claimed_at = _ensure_aware(
                        datetime.fromisoformat(existing_claim["at"])
                    )
                    # 0 <= age: a future-dated claim (clock/TZ skew across a
                    # restart) must be treated as stale, not eternally fresh,
                    # or the one-shot is skipped forever (#60703).
                    _age = (now - claimed_at).total_seconds()
                    if 0 <= _age < _run_claim_ttl:
                        continue  # a fresh claim is held by an in-flight run
                except (KeyError, ValueError, TypeError):
                    pass  # malformed claim → fall through and (re)claim

            next_run = job.get("next_run_at")
            if not next_run:
                schedule = job.get("schedule", {})
                kind = schedule.get("kind")

                # One-shot jobs use a small grace window via the dedicated helper.
                recovered_next = _recoverable_oneshot_run_at(
                    schedule,
                    now,
                    last_run_at=job.get("last_run_at"),
                )
                recovery_kind = "one-shot" if recovered_next else None

                # Recurring jobs reach here only when something — typically a
                # direct jobs.json edit that bypassed add_job() — left
                # next_run_at unset.  Without this branch, such jobs are
                # silently skipped forever; recompute next_run_at from the
                # schedule so they pick up at their next scheduled tick.
                if not recovered_next and kind in {"cron", "interval"}:
                    recovered_next = compute_next_run(schedule, now.isoformat())
                    if recovered_next:
                        recovery_kind = kind

                if not recovered_next:
                    continue

                job["next_run_at"] = recovered_next
                next_run = recovered_next
                logger.info(
                    "Job '%s' had no next_run_at; recovering %s run at %s",
                    job.get("name", job.get("id", "?")),
                    recovery_kind,
                    recovered_next,
                )
                for rj in raw_jobs:
                    if rj["id"] == job["id"]:
                        rj["next_run_at"] = recovered_next
                        needs_save = True
                        break

            raw_next_run_dt = datetime.fromisoformat(next_run)
            schedule = job.get("schedule", {})
            kind = schedule.get("kind")

            next_run_dt = _ensure_aware(raw_next_run_dt)
            # Migration repair: a cron job persists next_run_at as an absolute
            # instant, but the cron expr describes local wall-clock intent. If the
            # configured/system timezone changed after persistence, the stored
            # instant's offset no longer matches now's, and its converted time can
            # look due hours early (21:00+10 -> 13:00+02). When the stored *wall
            # clock* is still in the future, recompute from the schedule so we fire
            # at the intended local time instead of early-then-again.
            #
            # TRADE-OFF: this cannot distinguish a config/host TZ migration from a
            # legitimate DST offset change. A DST boundary that satisfies all four
            # conditions will recompute (and thus SKIP the pending occurrence, no
            # catch-up) rather than fire it. Accepted: in the pure-migration case
            # the recompute lands on the same wall-clock time later the same period,
            # and DST-boundary collisions with a still-future stored wall clock are
            # rare relative to the double-fire bug this prevents (#28934).
            if (
                kind == "cron"
                and next_run_dt <= now
                and _timezone_offset_mismatch(raw_next_run_dt, now)
                and _stored_wall_clock_is_future(raw_next_run_dt, now)
            ):
                new_next = compute_next_run(schedule, now.isoformat())
                if new_next:
                    logger.info(
                        "Job '%s' next_run_at offset changed (%s -> %s). "
                        "Recomputing cron run to preserve local wall-clock intent: %s",
                        job.get("name", job.get("id", "?")),
                        raw_next_run_dt.utcoffset(),
                        now.utcoffset(),
                        new_next,
                    )
                    for rj in raw_jobs:
                        if rj["id"] == job["id"]:
                            rj["next_run_at"] = new_next
                            needs_save = True
                            break
                    continue

            if next_run_dt <= now:

                # For recurring jobs, check if the scheduled time is stale
                # (gateway was down and missed the window). Fast-forward to
                # the next future occurrence instead of firing a stale run.
                grace = _compute_grace_seconds(schedule)
                if kind in {"cron", "interval"} and (now - next_run_dt).total_seconds() > grace:
                    # Job is past its catch-up grace window — skip accumulated
                    # missed runs but still execute once now to avoid deferring
                    # indefinitely (e.g. a long-running job just finished).
                    new_next = compute_next_run(schedule, now.isoformat())
                    if new_next:
                        logger.info(
                            "Job '%s' missed its scheduled time (%s, grace=%ds). "
                            "Running now; next run provisionally set to: %s "
                            "(re-anchored on completion)",
                            job.get("name", job.get("id", "?")),
                            next_run,
                            grace,
                            new_next,
                        )
                        # Persist the fast-forward to storage now (skip accumulated
                        # slots). In the built-in ticker path this is shortly
                        # overwritten by advance_next_run + mark_job_run, but it is
                        # NOT redundant: it (a) protects the crash window between
                        # here and mark_job_run, and (b) covers the external
                        # fire_due provider path, which does not call
                        # advance_next_run. mark_job_run re-anchors next_run_at off
                        # the actual completion time, so this value is provisional.
                        for rj in raw_jobs:
                            if rj["id"] == job["id"]:
                                rj["next_run_at"] = new_next
                                needs_save = True
                                break
                        # Fall through to due.append(job) — execute once now

                # One-shot dispatch-limit guard (issue #38758): a finite one-shot
                # claimed via claim_dispatch() but whose tick died before
                # mark_job_run could remove it will have completed >= times while
                # still looking due (last_run_at was never written, so the
                # recovery helper re-armed it). Remove it instead of re-firing.
                if kind == "once":
                    repeat = job.get("repeat")
                    if repeat:
                        times = repeat.get("times")
                        completed = repeat.get("completed", 0)
                        if times is not None and times > 0 and completed >= times:
                            # A live run must never have its job record deleted
                            # underneath it (#62002): a run that outlives the
                            # run_claim TTL (stream stall, laptop asleep
                            # mid-run) satisfies the same completed >= times +
                            # expired-claim condition as a dead tick, but
                            # mark_job_run() still needs the record to land
                            # last_run_at / last_status / last_delivery_error.
                            # If this process is still running the job, it is
                            # slow, not stale — keep the entry and skip.
                            if _job_running_in_this_process(job.get("id", "")):
                                logger.info(
                                    "Job '%s': dispatch limit reached (%d/%d) "
                                    "but its run is still in flight in this "
                                    "process — keeping entry",
                                    job.get("name", job.get("id", "?")),
                                    completed,
                                    times,
                                )
                                continue
                            logger.info(
                                "Job '%s': one-shot dispatch limit reached (%d/%d) "
                                "— removing stale due entry",
                                job.get("name", job.get("id", "?")),
                                completed,
                                times,
                            )
                            for rj in raw_jobs:
                                if rj["id"] == job["id"]:
                                    raw_jobs.remove(rj)
                                    needs_save = True
                                    break
                            continue

                # Durably claim a one-shot for the DURATION of its run before
                # returning it as due, so a second scheduler process (gateway +
                # desktop both run in-process 60s tickers on one HERMES_HOME)
                # cannot re-dispatch it while the first run is still in flight
                # (#59229). A plain one-shot's due-state is not resolved until
                # mark_job_run() completes it minutes later, so advancing
                # next_run_at by a fixed window is not enough — a job that outlives
                # one tick (e.g. a 2.5-min research prompt) would simply re-fire on
                # the next tick after the window. Instead we stamp a run_claim under
                # the same lock get_due_jobs already holds; the other process reads
                # a fresh claim on its next tick and skips (handled at the top of
                # this loop). mark_job_run() clears the claim on completion. The TTL
                # is only a safety valve: a claiming tick that DIES mid-run leaves a
                # stale claim that expires after the resolved run-claim TTL
                # (_oneshot_run_claim_ttl_seconds, derived from HERMES_CRON_TIMEOUT),
                # so the job is re-dispatched rather than wedged forever.
                if kind == "once":
                    claim = {"at": now.isoformat(), "by": _machine_id()}
                    job["run_claim"] = claim
                    for rj in raw_jobs:
                        if rj["id"] == job["id"]:
                            rj["run_claim"] = claim
                            needs_save = True
                            break

                due.append(job)
        except Exception:
            logger.exception(
                "Skipping malformed cron job %r during due scan",
                job.get("name") or job.get("id") or "?",
            )
            continue

    if needs_save:
        save_jobs(raw_jobs)

    return due


def _write_job_output_file(output_file: Path, output: str) -> Path:
    """Atomically replace a reserved cron output artifact."""
    job_output_dir = output_file.parent
    job_output_dir.mkdir(parents=True, exist_ok=True)
    _secure_dir(job_output_dir)

    fd, tmp_path = tempfile.mkstemp(dir=str(job_output_dir), suffix='.tmp', prefix='.output_')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(output)
            f.flush()
            os.fsync(f.fileno())
        atomic_replace(tmp_path, output_file)
        _secure_file(output_file)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return output_file


def reserve_job_output(job_id: str, output: str = "") -> Path:
    """Reserve and optionally seed a cron output artifact path for one run."""
    ensure_dirs()
    job_output_dir = _job_output_dir(job_id)
    job_output_dir.mkdir(parents=True, exist_ok=True)
    _secure_dir(job_output_dir)

    timestamp = _hermes_now().strftime("%Y-%m-%d_%H-%M-%S")
    output_file = job_output_dir / f"{timestamp}.md"

    return _write_job_output_file(output_file, output)


def update_job_output(output_file: Union[str, Path], output: str) -> Path:
    """Atomically write final cron output to a reserved artifact path."""
    output_path = Path(output_file)
    return _write_job_output_file(output_path, output)


def save_job_output(job_id: str, output: str):
    """Save job output to file."""
    return reserve_job_output(job_id, output)


# =============================================================================
# Skill reference rewriting (curator integration)
# =============================================================================

def rewrite_skill_refs(
    consolidated: Optional[Dict[str, str]] = None,
    pruned: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Rewrite cron job skill references after a curator consolidation pass.

    When the curator consolidates a skill X into umbrella Y (or archives X
    as pruned), any cron job that lists ``X`` in its ``skills`` field will
    fail to load ``X`` at run time — the scheduler logs a warning and
    skips the skill, so the job runs without the instructions it was
    scheduled to follow. See cron/scheduler.py where ``skill_view`` is
    called per skill name.

    This function repairs cron jobs in-place:

    - A skill listed in ``consolidated`` is replaced with its umbrella
      target (the ``into`` value). If the umbrella is already in the
      job's skill list, the stale name is dropped without duplication.
    - A skill listed in ``pruned`` is dropped outright — there is no
      forwarding target.
    - Ordering and other skills in the list are preserved.
    - The legacy ``skill`` field is realigned via ``_apply_skill_fields``.

    Args:
        consolidated: mapping of ``old_skill_name -> umbrella_skill_name``.
        pruned: list of skill names that were archived with no forwarding
            target.

    Returns a report dict::

        {
            "rewrites": [
                {
                    "job_id": ...,
                    "job_name": ...,
                    "before": [...],
                    "after": [...],
                    "mapped": {"old": "new", ...},
                    "dropped": ["old", ...],
                },
                ...
            ],
            "jobs_updated": N,
            "jobs_scanned": M,
        }

    Best-effort: exceptions from loading/saving propagate to the caller so
    tests can assert behaviour; the curator invocation site wraps this
    call in a try/except so a failure here never breaks the curator.
    """
    consolidated = dict(consolidated or {})
    pruned_set = set(pruned or [])
    # A skill listed in both wins as "consolidated" — it has a target,
    # which is the more useful of the two outcomes.
    pruned_set -= set(consolidated.keys())

    if not consolidated and not pruned_set:
        return {"rewrites": [], "jobs_updated": 0, "jobs_scanned": 0}

    with _jobs_file_lock:
        jobs = load_jobs()
        rewrites: List[Dict[str, Any]] = []
        changed = False

        for job in jobs:
            skills_before = _normalize_skill_list(job.get("skill"), job.get("skills"))
            if not skills_before:
                continue

            mapped: Dict[str, str] = {}
            dropped: List[str] = []
            new_skills: List[str] = []

            for name in skills_before:
                if name in consolidated:
                    target = consolidated[name]
                    mapped[name] = target
                    if target and target not in new_skills:
                        new_skills.append(target)
                elif name in pruned_set:
                    dropped.append(name)
                elif name not in new_skills:
                    new_skills.append(name)

            if not mapped and not dropped:
                continue

            job["skills"] = new_skills
            job["skill"] = new_skills[0] if new_skills else None
            changed = True

            rewrites.append({
                "job_id": job.get("id"),
                "job_name": job.get("name") or job.get("id"),
                "before": list(skills_before),
                "after": list(new_skills),
                "mapped": mapped,
                "dropped": dropped,
            })

        if changed:
            save_jobs(jobs)
            logger.info(
                "Curator rewrote skill references in %d cron job(s)", len(rewrites)
            )

        return {
            "rewrites": rewrites,
            "jobs_updated": len(rewrites),
            "jobs_scanned": len(jobs),
        }
