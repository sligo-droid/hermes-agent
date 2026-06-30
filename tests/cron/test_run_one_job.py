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

    s.run_one_job({"id": "j4", "name": "t"})

    mark = [c for c in calls if c[0] == "mark"][0]
    assert mark == ("mark", "j4", False)


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


def test_run_one_job_malformed_self_improvement_ingestion_records_health(monkeypatch):
    calls = _patch_pipeline(monkeypatch)
    seen_health_details = []

    def fake_mark(jid, ok, err=None, delivery_error=None, health_details=None):
        calls.append(("mark", jid, ok))
        seen_health_details.append(health_details)

    parse_error = "proposal JSON parse error at line 1, column 2: no secret payload"
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

    ok = s.run_one_job({"id": "j8", "name": "t", "self_improvement_proposal": {"project": "p", "prong": "q"}})

    assert ok is True
    detail = seen_health_details[0]["self_improvement_proposal_ingestion"]
    assert detail == {
        "status": "malformed",
        "card_count": 0,
        "parse_error": parse_error,
        "cron_output_path": "/tmp/j8.txt",
        "source_key": "cron:j8:j8.txt",
        "run_id": 18,
    }


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
