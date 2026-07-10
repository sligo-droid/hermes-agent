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

    def fake_run_job(job):
        calls.append(("run_job", job["id"]))
        fr = final if silent_marker_in is None else silent_marker_in
        return (success, output, fr, error)

    def fake_save(jid, out):
        calls.append(("save", jid))
        return f"/tmp/{jid}.txt"

    def fake_deliver(job, content, adapters=None, loop=None):
        calls.append(("deliver", job["id"]))
        return None

    def fake_mark(jid, ok, err=None, delivery_error=None, health_details=None):
        calls.append(("mark", jid, ok))

    monkeypatch.setattr(s, "run_job", fake_run_job)
    monkeypatch.setattr(s, "save_job_output", fake_save)
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

    assert [c[0] for c in calls] == ["run_job", "save", "deliver", "mark"]
    assert calls[-1] == ("mark", "j1", True)


def test_run_one_job_success_sequence(monkeypatch):
    """The extracted helper runs the same execute→save→deliver→mark sequence
    for a successful job."""
    calls = _patch_pipeline(monkeypatch)

    ok = s.run_one_job({"id": "j2", "name": "t"})

    assert ok is True
    assert [c[0] for c in calls] == ["run_job", "save", "deliver", "mark"]
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

    assert ok is False
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
    def boom(job):
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


def test_run_one_job_malformed_self_improvement_ingestion_marks_failure(monkeypatch):
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

    assert ok is False
    assert calls[-1] == ("mark", "j8", False)
    assert seen_errors == [f"Self-improvement proposal ingestion failed: {parse_error}"]
    assert deliveries == [f"⚠️ Cron job 't' failed:\nSelf-improvement proposal ingestion failed: {parse_error}"]
    assert manual_finishes == [
        (
            "j8",
            "manual-8",
            {
                "success": False,
                "output_path": "/tmp/j8.txt",
                "error": f"Self-improvement proposal ingestion failed: {parse_error}",
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

    assert ok is False
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

    assert ok is False
    assert seen_health_details == [None]
