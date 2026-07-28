"""Compatibility tests for session-aware cache count re-baselining.

Bug
---
The cache key is the gateway ``session_key`` (for example,
``agent:main:telegram:dm:USER_ID``), so separate DB session IDs can occupy the
same cache slot over time. A tuple/session mismatch is reusable only when the
live agent has already rotated to the incoming session ID. Otherwise the agent
still owns the old conversation and must be rebuilt, regardless of whether the
old DB row remains live.

These tests pin the production guard's reuse decision across:
  L1 — a legitimately rotated agent may be reused across a stale tuple ID.
  L2 — cache tuple records the snapshot's session_id.
  L3 — re-baseline skips the cache entry when session_id differs.
  L4 — same-session_id turns still re-baseline correctly (no regression
       of #45966 / #46237).
  L5 — legacy 2-tuples and pending sentinels are still untouched.

The cache-hit ownership decisions themselves are covered through the live
``GatewayRunner._run_agent`` path in ``test_run_agent_cache_live_path.py``.
These tests retain focused coverage of the production re-baseline helper and
legacy tuple compatibility against a real ``SessionDB``.
"""

import threading
from types import SimpleNamespace

import pytest

from hermes_state import AsyncSessionDB


def _make_runner():
    """Create a minimal GatewayRunner with just the cache infrastructure."""
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()
    return runner


def _guard_would_reuse(runner, session_key, session_id):
    """Mirror the production cache-hit guard's reuse decision exactly
    for focused count-coherence assertions.

    Reuse iff any of:
      - cached session_id matches current session_id AND live count matches
        snapshot count (same-process turn OR no foreign write)
      - cached session_id differs from current session_id AND the live agent
        already owns the incoming session (legitimate agent-side rotation)
      - entry is a 2-tuple (legacy opt-out of the count guard) and live agent
        ownership does not contradict the incoming session

    Invalidate iff:
      - the live agent has a different session_id from the incoming session
      - a tuple/session mismatch cannot be validated by live agent ownership
      - cached session_id == current session_id AND cached/live counts differ
        (genuine cross-process write on the same session).
    """
    try:
        # Mirror the production guard, which reads the sync underlying DB
        # (``self._session_db._db.get_session``) off the async facade.
        row = runner._session_db._db.get_session(session_id)
        live = row.get("message_count", 0) if row else None
    except Exception:
        live = None
    with runner._agent_cache_lock:
        cached = runner._agent_cache.get(session_key)

    if cached is None:
        return True  # no entry → cache miss → fresh build (not invalidation)
    cached_agent_sid = str(getattr(cached[0], "session_id", "") or "")
    incoming_sid = str(session_id or "")
    if cached_agent_sid and incoming_sid and cached_agent_sid != incoming_sid:
        return False

    # Legacy 2-tuple opts out of the count guard. It has no tuple ownership
    # snapshot, so only a live agent.session_id can disprove ownership.
    if len(cached) < 3:
        return True
    cached_sid = cached[3] if len(cached) > 3 else None
    cached_mc = cached[2]

    # A stale tuple belongs to another DB row. Reuse only when the live agent
    # has already rotated to and therefore owns the incoming session.
    if cached_sid is not None and session_id is not None and cached_sid != session_id:
        return cached_agent_sid == incoming_sid

    # Same session_id: standard cross-process guard.
    invalidate = (
        cached_mc is not None
        and live is not None
        and live != cached_mc
    )
    return not invalidate


class TestSessionIdCacheCoherence:
    """#54947 — session-aware count coherence compatibility."""

    def test_rotated_agent_reuses_stale_tuple_for_owned_session(self, tmp_path):
        """A live agent already rotated to B may adopt a tuple still naming A."""
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "sessions.db")
        db.create_session("sA", source="telegram")
        db.create_session("sB", source="telegram")
        # Make counts differ to make the bug observable.
        db.append_message("sA", role="user", content="hello from A")
        db.append_message("sA", role="assistant", content="hi A")
        db.append_message("sA", role="user", content="another from A")
        # sA count = 3, sB count = 0
        runner = _make_runner()
        runner._session_db = AsyncSessionDB(db)
        agent = SimpleNamespace(session_id="sB")

        # Build cache from session A (mc=3, sid=sA).
        with runner._agent_cache_lock:
            runner._agent_cache["telegram:USER1"] = (agent, "sig", 3, "sA")

        # The agent has rotated to B, while the tuple still records A.
        assert _guard_would_reuse(runner, "telegram:USER1", "sB") is True, (
            "BUG: cache rejected an agent that already owns the incoming session."
        )
        # The original agent must still be in the cache.
        with runner._agent_cache_lock:
            assert runner._agent_cache["telegram:USER1"][0] is agent

    @pytest.mark.asyncio
    async def test_same_session_id_turns_still_reuse(self, tmp_path):
        """#46237 / #45966 invariant: consecutive same-session turns must
        REUSE the cached agent (prompt cache preserved)."""
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "sessions.db")
        db.create_session("s1", source="telegram")
        runner = _make_runner()
        runner._session_db = AsyncSessionDB(db)
        agent = object()

        _row = db.get_session("s1")
        build_count = _row.get("message_count", 0) if _row else 0
        with runner._agent_cache_lock:
            runner._agent_cache["telegram:s1"] = (agent, "sig", build_count, "s1")

        reuses = 0
        for _ in range(1, 6):
            db.append_message("s1", role="user", content="u")
            db.append_message("s1", role="assistant", content="a")
            # Post-turn re-baseline (the #46237 fix).
            await runner._refresh_agent_cache_message_count("telegram:s1", "s1")
            if _guard_would_reuse(runner, "telegram:s1", "s1"):
                reuses += 1

        assert reuses == 5
        with runner._agent_cache_lock:
            assert runner._agent_cache["telegram:s1"][0] is agent

    @pytest.mark.asyncio
    async def test_cross_process_write_still_invalidates(self, tmp_path):
        """The original #45966 invariant must hold: a DIFFERENT process
        appending to the same session in the shared DB invalidates the
        cache (genuine cross-process write)."""
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "sessions.db")
        db.create_session("s1", source="telegram")
        runner = _make_runner()
        runner._session_db = AsyncSessionDB(db)
        agent = object()

        with runner._agent_cache_lock:
            _row = db.get_session("s1")
            runner._agent_cache["telegram:s1"] = (
                agent, "sig", (_row.get("message_count", 0) if _row else 0), "s1",
            )

        # Our own turn + re-baseline → reuse next turn.
        db.append_message("s1", role="user", content="u")
        db.append_message("s1", role="assistant", content="a")
        await runner._refresh_agent_cache_message_count("telegram:s1", "s1")
        assert _guard_would_reuse(runner, "telegram:s1", "s1") is True

        # ANOTHER process (e.g. the desktop dashboard backend) appends a
        # turn to the SAME session in the shared DB.
        db.append_message("s1", role="user", content="external from dashboard")

        # Guard must invalidate.
        assert _guard_would_reuse(runner, "telegram:s1", "s1") is False

    @pytest.mark.asyncio
    async def test_refresh_skips_mismatch_without_agent_ownership(self, tmp_path):
        """A stale tuple cannot adopt a new row without agent ownership proof."""
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "sessions.db")
        db.create_session("sA", source="telegram")
        db.create_session("sB", source="telegram")
        db.append_message("sA", role="user", content="x")
        runner = _make_runner()
        runner._session_db = AsyncSessionDB(db)
        agent = object()

        # Cache built from session A: (agent, sig, mc=1, sid=sA).
        with runner._agent_cache_lock:
            runner._agent_cache["telegram:USER1"] = (agent, "sig", 1, "sA")

        # The tuple names A and this compatibility fake exposes no live
        # agent.session_id proving that it has rotated to B.
        await runner._refresh_agent_cache_message_count("telegram:USER1", "sB")

        with runner._agent_cache_lock:
            cached = runner._agent_cache["telegram:USER1"]
            assert cached[2] == 1, (
                f"BUG: snapshot was overwritten with sB's count: cached[2]={cached[2]}"
            )
            assert cached[3] == "sA", (
                f"BUG: snapshot's session_id was changed: cached[3]={cached[3]}"
            )
            assert cached[0] is agent

    @pytest.mark.asyncio
    async def test_refresh_refreshes_when_session_id_matches(self, tmp_path):
        """Sanity: when the snapshot's session_id matches the current one,
        the re-baseline still runs and updates the count to the live value."""
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "sessions.db")
        db.create_session("s1", source="telegram")
        runner = _make_runner()
        runner._session_db = AsyncSessionDB(db)
        agent = object()

        with runner._agent_cache_lock:
            runner._agent_cache["telegram:s1"] = (agent, "sig", 0, "s1")

        # s1's own turn flushes two rows.
        db.append_message("s1", role="user", content="u")
        db.append_message("s1", role="assistant", content="a")
        await runner._refresh_agent_cache_message_count("telegram:s1", "s1")

        with runner._agent_cache_lock:
            assert runner._agent_cache["telegram:s1"][2] == 2

    @pytest.mark.asyncio
    async def test_legacy_2tuple_and_pending_sentinel_untouched(self, tmp_path):
        """Backward-compat: legacy 2-tuples and pending-sentinel 3-tuples
        are not affected by the fix. The 2-tuple opts out of the guard;
        the sentinel is left as-is by the re-baseline."""
        from hermes_state import SessionDB
        from gateway.run import _AGENT_PENDING_SENTINEL

        db = SessionDB(db_path=tmp_path / "sessions.db")
        db.create_session("s1", source="telegram")
        db.append_message("s1", role="user", content="hi")
        runner = _make_runner()
        runner._session_db = AsyncSessionDB(db)

        # Legacy 2-tuple — untouched.
        with runner._agent_cache_lock:
            runner._agent_cache["telegram:s1"] = (object(), "sig")
        await runner._refresh_agent_cache_message_count("telegram:s1", "s1")
        with runner._agent_cache_lock:
            assert len(runner._agent_cache["telegram:s1"]) == 2

        # Pending sentinel — untouched.
        with runner._agent_cache_lock:
            runner._agent_cache["telegram:s1"] = (_AGENT_PENDING_SENTINEL, "sig", 0)
        await runner._refresh_agent_cache_message_count("telegram:s1", "s1")
        with runner._agent_cache_lock:
            assert runner._agent_cache["telegram:s1"][0] is _AGENT_PENDING_SENTINEL
            assert runner._agent_cache["telegram:s1"][2] == 0

    @pytest.mark.asyncio
    async def test_legacy_3tuple_session_id_unknown_still_guarded(self, tmp_path):
        """An entry in the OLD 3-tuple shape (agent, sig, mc) with no
        session_id — entries already in the cache from BEFORE the fix —
        must STILL be guarded by the cross-process check.  The fix only
        ADDS a session_id-aware skip path; it does not weaken the
        existing #45966 guard for entries that pre-date it.  When
        live count != snapshot count, the guard fires and the agent
        rebuilds (same behavior as before the fix for legacy entries).
        """
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "sessions.db")
        db.create_session("s1", source="telegram")
        db.append_message("s1", role="user", content="x")
        runner = _make_runner()
        runner._session_db = AsyncSessionDB(db)

        # Existing entry in old 3-tuple shape, no session_id recorded.
        # Snapshot is mc=0; live is mc=1 — guard must fire (invalidate).
        with runner._agent_cache_lock:
            runner._agent_cache["telegram:s1"] = (object(), "sig", 0)

        # No session_id on cached entry → standard cross-process check
        # still runs.  live (1) != snapshot (0) → invalidates.
        assert _guard_would_reuse(runner, "telegram:s1", "s1") is False

        # After the re-baseline (same session_id) snapshot matches live.
        await runner._refresh_agent_cache_message_count("telegram:s1", "s1")
        with runner._agent_cache_lock:
            assert runner._agent_cache["telegram:s1"][2] == 1
        assert _guard_would_reuse(runner, "telegram:s1", "s1") is True
