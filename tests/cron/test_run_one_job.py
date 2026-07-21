"""Characterization + unit tests for the `run_one_job` shared helper (Phase 4A).

`tick`'s per-job body (`_process_job`) is the execute → save → deliver → mark
sequence that fires ONE due job. Phase 4A extracts it into a module-level
`run_one_job(job, *, adapters=None, loop=None, verbose=False)` so the external
Chronos provider's `fire_due` can reuse the IDENTICAL body — no duplicated
correctness.

The first test characterizes the sequence as driven through `tick()` (proving
the extraction didn't change `tick`'s behavior); the rest unit-test the
extracted helper directly.
"""
from pathlib import Path

import cron.scheduler as s


def _patch_pipeline(monkeypatch, *, success=True, output="out", final="final response",
                    error=None, silent_marker_in=None):
    """Patch the job pipeline primitives and record the call order."""
    calls = []

    def fake_run_job(job, *, defer_agent_teardown=None):
        calls.append(("run_job", job["id"]))
        fr = final if silent_marker_in is None else silent_marker_in
        return (success, output, fr, error)

    def fake_save(jid, out):
        calls.append(("save", jid))
        return f"/tmp/{jid}.txt"

    def fake_update(path, out):
        calls.append(("update", path))

    def fake_deliver(job, content, adapters=None, loop=None):
        calls.append(("deliver", job["id"]))
        return None

    def fake_mark(jid, ok, err=None, delivery_error=None, health_details=None):
        calls.append(("mark", jid, ok))

    monkeypatch.setattr(s, "run_job", fake_run_job)
    monkeypatch.setattr(s, "save_job_output", fake_save)
    monkeypatch.setattr(s, "update_job_output", fake_update)
    monkeypatch.setattr(s, "_deliver_result", fake_deliver)
    monkeypatch.setattr(s, "mark_job_run", fake_mark)
    return calls


def test_tick_process_job_sequence(monkeypatch):
    """Characterization: a single due job driven through tick() runs the
    sequence run_job → save → deliver → mark, in that order."""
    calls = _patch_pipeline(monkeypatch)
    monkeypatch.setattr(s, "get_due_jobs", lambda: [{"id": "j1", "name": "t"}])
    monkeypatch.setattr(s, "advance_next_run", lambda jid: True)

    s.tick(verbose=False, sync=True)

    assert [c[0] for c in calls] == ["save", "run_job", "update", "deliver", "mark"]
    assert calls[-1] == ("mark", "j1", True)


def test_run_one_job_success_sequence(monkeypatch):
    """The extracted helper runs the same execute→save→deliver→mark sequence
    for a successful job."""
    calls = _patch_pipeline(monkeypatch)

    ok = s.run_one_job({"id": "j2", "name": "t"})

    assert ok is True
    assert [c[0] for c in calls] == ["save", "run_job", "update", "deliver", "mark"]
    assert calls[-1] == ("mark", "j2", True)


def test_run_one_job_silent_skips_delivery(monkeypatch):
    """A [SILENT] final response saves output + marks the run but does NOT
    deliver."""
    calls = _patch_pipeline(monkeypatch, silent_marker_in="[SILENT]")

    s.run_one_job({"id": "j3", "name": "t"})

    kinds = [c[0] for c in calls]
    assert "run_job" in kinds and "save" in kinds and "mark" in kinds
    assert "deliver" not in kinds


def test_run_one_job_empty_response_is_soft_failure(monkeypatch):
    """An empty final response marks the run as NOT ok (issue #8585)."""
    calls = _patch_pipeline(monkeypatch, final="   ")
    manual_finishes = []
    job_marks = []
    empty_response_error = "Agent completed but produced empty response (model error, timeout, or misconfiguration)"
    monkeypatch.setattr(s, "mark_manual_run_started", lambda *args: None)
    monkeypatch.setattr(
        s,
        "mark_manual_run_finished",
        lambda jid, run_id, **kwargs: manual_finishes.append((jid, run_id, kwargs)),
    )
    monkeypatch.setattr(
        s,
        "mark_job_run",
        lambda jid, ok, err=None, delivery_error=None, health_details=None: job_marks.append(
            (jid, ok, err, delivery_error, health_details)
        ),
    )

    ok = s.run_one_job({
        "id": "j4",
        "name": "t",
        "manual_run": {"run_id": "manual-4", "state": "queued"},
    })

    assert ok is True  # processed successfully; job outcome is recorded as failure
    assert "deliver" not in [call[0] for call in calls]
    assert manual_finishes == [
        (
            "j4",
            "manual-4",
            {
                "success": False,
                "output_path": "/tmp/j4.txt",
                "error": empty_response_error,
            },
        )
    ]
    assert job_marks == [("j4", False, empty_response_error, None, None)]


def test_run_one_job_failed_job_delivers_error(monkeypatch):
    """A failed job still delivers (the error notice) and marks not-ok."""
    calls = _patch_pipeline(monkeypatch, success=False, final="", error="boom")

    s.run_one_job({"id": "j5", "name": "t"})

    kinds = [c[0] for c in calls]
    assert "deliver" in kinds  # failures always deliver
    mark = [c for c in calls if c[0] == "mark"][0]
    assert mark == ("mark", "j5", False)


def test_run_one_job_exception_marks_failure(monkeypatch):
    """If run_job raises, the helper marks the run failed and returns False
    rather than propagating."""
    def boom(job, *, defer_agent_teardown=None):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(s, "run_job", boom)
    marks = []
    monkeypatch.setattr(
        s, "mark_job_run",
        lambda jid, ok, err=None, delivery_error=None, health_details=None: marks.append((jid, ok)),
    )

    ok = s.run_one_job({"id": "j6", "name": "t"})

    assert ok is False
    assert marks == [("j6", False)]


def test_run_one_job_valid_self_improvement_ingestion_keeps_health_ok(monkeypatch):
    calls = _patch_pipeline(monkeypatch)
    seen_health_details = []

    def fake_mark(jid, ok, err=None, delivery_error=None, health_details=None):
        calls.append(("mark", jid, ok))
        seen_health_details.append(health_details)

    monkeypatch.setattr(s, "mark_job_run", fake_mark)
    monkeypatch.setattr(
        s,
        "_ingest_self_improvement_proposal_output",
        lambda job, output, output_file, final_response: {
            "status": "valid",
            "card_count": 1,
            "parse_error": None,
            "source_key": "cron:j7:j7.txt",
            "run_id": 17,
        },
    )

    ok = s.run_one_job({"id": "j7", "name": "t", "self_improvement_proposal": {"project": "p", "prong": "q"}})

    assert ok is True
    assert seen_health_details == [None]


def test_run_one_job_malformed_self_improvement_ingestion_preserves_doctor_success(monkeypatch):
    calls = _patch_pipeline(monkeypatch)
    deliveries = []
    manual_finishes = []
    seen_health_details = []
    seen_errors = []

    def fake_mark(jid, ok, err=None, delivery_error=None, health_details=None):
        calls.append(("mark", jid, ok))
        seen_errors.append(err)
        seen_health_details.append(health_details)

    parse_error = "proposal JSON parse error at line 1, column 2: no secret payload"
    monkeypatch.setattr(
        s,
        "_deliver_result",
        lambda job, content, adapters=None, loop=None: deliveries.append(content),
    )
    monkeypatch.setattr(s, "mark_manual_run_started", lambda *args: None)
    monkeypatch.setattr(
        s,
        "mark_manual_run_finished",
        lambda jid, run_id, **kwargs: manual_finishes.append((jid, run_id, kwargs)),
    )
    monkeypatch.setattr(s, "mark_job_run", fake_mark)
    monkeypatch.setattr(
        s,
        "_ingest_self_improvement_proposal_output",
        lambda job, output, output_file, final_response: {
            "status": "malformed",
            "card_count": 0,
            "parse_error": parse_error,
            "source_key": "cron:j8:j8.txt",
            "run_id": 18,
        },
    )

    ok = s.run_one_job({
        "id": "j8",
        "name": "t",
        "manual_run": {"run_id": "manual-8", "state": "queued"},
        "self_improvement_proposal": {"project": "p", "prong": "q"},
    })

    assert ok is True
    assert calls[-1] == ("mark", "j8", True)
    assert seen_errors == [None]
    assert deliveries == ["final response"]
    assert manual_finishes == [
        (
            "j8",
            "manual-8",
            {
                "success": True,
                "output_path": "/tmp/j8.txt",
                "error": None,
            },
        )
    ]
    detail = seen_health_details[0]["self_improvement_proposal_ingestion"]
    assert detail == {
        "status": "malformed",
        "card_count": 0,
        "parse_error": parse_error,
        "cron_output_path": "/tmp/j8.txt",
        "source_key": "cron:j8:j8.txt",
        "run_id": 18,
    }


def test_run_one_job_valid_zero_card_self_improvement_ingestion_keeps_health_ok(monkeypatch, tmp_path):
    empty_proposal = (
        Path(__file__).parents[1] / "fixtures" / "self_improvement" / "proposal_run_pid_empty.json"
    ).read_text(encoding="utf-8")
    calls = _patch_pipeline(monkeypatch, output=empty_proposal, final=empty_proposal)
    seen_health_details = []

    def fake_mark(jid, ok, err=None, delivery_error=None, health_details=None):
        calls.append(("mark", jid, ok))
        seen_health_details.append(health_details)

    monkeypatch.setattr(s, "mark_job_run", fake_mark)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))

    ok = s.run_one_job(
        {
            "id": "j-empty",
            "name": "t",
            "self_improvement_proposal": {"project": "pid", "prong": "visible_ui_ux_recommendations"},
        }
    )

    assert ok is True
    assert calls[-1] == ("mark", "j-empty", True)
    assert seen_health_details[0]["self_improvement_proposal_ingestion"]["status"] == "empty"


def test_run_one_job_auth_blocked_records_health_without_ingesting_output(monkeypatch):
    calls = _patch_pipeline(
        monkeypatch,
        success=False,
        output="failure output",
        final="",
        error="RuntimeError: OpenAICodex upstream HTTP 401 token_invalidated",
    )
    seen_health_details = []

    def fake_mark(jid, ok, err=None, delivery_error=None, health_details=None):
        calls.append(("mark", jid, ok))
        seen_health_details.append(health_details)

    monkeypatch.setattr(s, "mark_job_run", fake_mark)
    monkeypatch.setattr(
        s,
        "_ingest_self_improvement_proposal_output",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not ingest failed output")),
    )
    monkeypatch.setattr(
        s,
        "_record_self_improvement_auth_blocked_run",
        lambda job, output_file, failure: {
            "status": "auth_blocked",
            "card_count": 0,
            "parse_error": failure.summary,
            "source_key": "cron:j-auth:j-auth.txt",
            "run_id": 19,
        },
    )

    ok = s.run_one_job({"id": "j-auth", "name": "t", "self_improvement_proposal": {"project": "p", "prong": "q"}})

    assert ok is True  # processed successfully; job outcome remains failed
    detail = seen_health_details[0]["self_improvement_proposal_ingestion"]
    assert detail["status"] == "auth_blocked"
    assert detail["card_count"] == 0
    assert detail["provider_class"] == "OpenAICodex"
    assert detail["failure_code"] == "token_invalidated"


def test_run_one_job_non_self_improvement_has_no_ingestion_health(monkeypatch):
    calls = _patch_pipeline(monkeypatch)
    seen_health_details = []

    def fake_mark(jid, ok, err=None, delivery_error=None, health_details=None):
        calls.append(("mark", jid, ok))
        seen_health_details.append(health_details)

    monkeypatch.setattr(s, "mark_job_run", fake_mark)

    ok = s.run_one_job({"id": "j9", "name": "t"})

    assert ok is True
    assert seen_health_details == [None]


def test_run_one_job_non_self_improvement_auth_failure_has_no_ingestion_health(monkeypatch):
    calls = _patch_pipeline(
        monkeypatch,
        success=False,
        output="failure output",
        final="",
        error="RuntimeError: OpenAICodex upstream HTTP 401 token_invalidated",
    )
    seen_health_details = []

    def fake_mark(jid, ok, err=None, delivery_error=None, health_details=None):
        calls.append(("mark", jid, ok))
        seen_health_details.append(health_details)

    monkeypatch.setattr(s, "mark_job_run", fake_mark)
    monkeypatch.setattr(
        s,
        "_record_self_improvement_auth_blocked_run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not record proposal status")),
    )

    ok = s.run_one_job({"id": "j-non-proposal", "name": "t"})

    assert ok is True  # processed successfully; job outcome remains failed
    assert seen_health_details == [None]


def test_run_one_job_installs_secret_scope_under_multiplex(monkeypatch, tmp_path):
    """run_one_job installs and tears down the profile secret scope."""
    from agent import secret_scope as ss

    (tmp_path / ".env").write_text(
        "OPENROUTER_BASE_URL=https://openrouter.ai/api/v1\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(s, "_get_hermes_home", lambda: tmp_path)

    scope_during_run = {}

    def fake_run_job(job, *, defer_agent_teardown=None):
        # This is where resolve_runtime_provider() would read a secret. Prove a
        # scope is installed and the profile's secret resolves without raising.
        scope_during_run["scope"] = ss.current_secret_scope()
        scope_during_run["base_url"] = ss.get_secret("OPENROUTER_BASE_URL")
        return (True, "out", "final", None)

    monkeypatch.setattr(s, "run_job", fake_run_job)
    monkeypatch.setattr(s, "save_job_output", lambda jid, out: f"/tmp/{jid}.txt")
    monkeypatch.setattr(s, "update_job_output", lambda path, out: None)
    monkeypatch.setattr(s, "_deliver_result", lambda *a, **k: None)
    monkeypatch.setattr(s, "mark_job_run", lambda *a, **k: None)

    ss.set_multiplex_active(True)
    try:
        ok = s.run_one_job({"id": "j7", "name": "t"})
    finally:
        ss.set_multiplex_active(False)

    assert ok is True
    # Scope was installed during run_job and the profile secret resolved.
    assert scope_during_run["scope"] is not None
    assert scope_during_run["base_url"] == "https://openrouter.ai/api/v1"
    # And it was torn down after run_one_job returned (no leak).
    assert ss.current_secret_scope() is None


def test_run_one_job_delivers_before_agent_teardown(monkeypatch):
    """Regression for #58720: the cron agent's async-resource teardown
    (agent.close + cleanup_stale_async_clients) MUST run AFTER delivery, not
    before. run_job defers teardown by appending the live agent to the holder
    list; run_one_job tears it down only after _deliver_result has run. If the
    order flips, delivery races a torn-down async client and dies with
    'cannot schedule new futures after interpreter shutdown'.
    """
    order = []

    class FakeAgent:
        def close(self):
            order.append("agent.close")

    def fake_run_job(job, *, defer_agent_teardown=None):
        order.append("run_job")
        # Mimic run_job's deferral contract: hand the live agent back so the
        # caller tears it down after delivery instead of in run_job's finally.
        assert defer_agent_teardown is not None, "run_one_job must defer teardown"
        defer_agent_teardown.append(FakeAgent())
        return (True, "out", "final response", None)

    def fake_deliver(job, content, adapters=None, loop=None):
        order.append("deliver")
        return None

    monkeypatch.setattr(s, "run_job", fake_run_job)
    monkeypatch.setattr(s, "save_job_output", lambda jid, out: f"/tmp/{jid}.txt")
    monkeypatch.setattr(s, "update_job_output", lambda path, out: None)
    monkeypatch.setattr(s, "_deliver_result", fake_deliver)
    monkeypatch.setattr(s, "mark_job_run", lambda *a, **k: None)
    # cleanup_stale_async_clients is imported lazily inside _teardown_cron_agent;
    # stub it so the teardown records its own marker without touching real caches.
    import agent.auxiliary_client as aux
    monkeypatch.setattr(aux, "cleanup_stale_async_clients",
                        lambda: order.append("cleanup_stale"))

    ok = s.run_one_job({"id": "j8", "name": "t"})

    assert ok is True
    # Delivery must strictly precede agent teardown + stale-client reap.
    assert order == ["run_job", "deliver", "agent.close", "cleanup_stale"], order


def test_run_one_job_tears_down_deferred_agent_when_delivery_raises(monkeypatch):
    """Even if _deliver_result raises, the deferred agent is still torn down
    (no fd/client leak — #10200). Teardown lives in a finally around delivery.
    """
    order = []

    class FakeAgent:
        def close(self):
            order.append("agent.close")

    def fake_run_job(job, *, defer_agent_teardown=None):
        defer_agent_teardown.append(FakeAgent())
        return (True, "out", "final response", None)

    def boom_deliver(job, content, adapters=None, loop=None):
        order.append("deliver-raise")
        raise RuntimeError("send blew up")

    monkeypatch.setattr(s, "run_job", fake_run_job)
    monkeypatch.setattr(s, "save_job_output", lambda jid, out: f"/tmp/{jid}.txt")
    monkeypatch.setattr(s, "_deliver_result", boom_deliver)
    monkeypatch.setattr(s, "mark_job_run", lambda *a, **k: None)
    import agent.auxiliary_client as aux
    monkeypatch.setattr(aux, "cleanup_stale_async_clients",
                        lambda: order.append("cleanup_stale"))

    ok = s.run_one_job({"id": "j9", "name": "t"})

    assert ok is True  # delivery error is recorded, not propagated
    assert order == ["deliver-raise", "agent.close", "cleanup_stale"], order


def test_run_one_job_tears_down_deferred_agent_when_update_raises(monkeypatch):
    """If final artifact closeout raises after run_job hands the agent back,
    the deferred agent must still be torn down before returning.
    """
    order = []

    class FakeAgent:
        def close(self):
            order.append("agent.close")

    def fake_run_job(job, *, defer_agent_teardown=None):
        defer_agent_teardown.append(FakeAgent())
        return (True, "out", "final response", None)

    def boom_update(path, out):
        order.append("update-raise")
        raise RuntimeError("disk full")

    monkeypatch.setattr(s, "run_job", fake_run_job)
    monkeypatch.setattr(s, "save_job_output", lambda jid, out: f"/tmp/{jid}.md")
    monkeypatch.setattr(s, "update_job_output", boom_update)
    monkeypatch.setattr(s, "_deliver_result",
                        lambda *a, **k: order.append("deliver"))
    monkeypatch.setattr(s, "mark_job_run", lambda *a, **k: None)
    import agent.auxiliary_client as aux
    monkeypatch.setattr(aux, "cleanup_stale_async_clients",
                        lambda: order.append("cleanup_stale"))

    ok = s.run_one_job({"id": "j10", "name": "t"})

    # closeout update raised → outer handler marks failure and returns False, but the
    # deferred agent was still torn down (no delivery, no leak).
    assert ok is False
    assert "deliver" not in order
    assert order == [
        "update-raise",
        "update-raise",
        "agent.close",
        "cleanup_stale",
    ], order
