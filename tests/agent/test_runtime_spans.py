from agent.runtime_spans import RuntimeSpanRecorder, summarize_span_intervals


def test_recorder_emits_bounded_safe_spans_without_raw_monotonic_values():
    recorder = RuntimeSpanRecorder(work_id="work-123", max_spans=2)
    handle = recorder.start(
        "model_attempt",
        phase="model",
        attempt_id="attempt-1",
        metadata={"model": "gpt-5", "secret": "do-not-store", "url": "https://private"},
    )
    recorder.finish(handle, status="ok")

    spans = recorder.export()
    assert len(spans) == 1
    span = spans[0]
    assert span["id"].startswith("span-")
    assert span["work_id"].startswith("wrk_")
    assert span["attempt_id"].startswith("att_")
    assert span["phase"] == "model"
    assert span["duration_s"] >= 0
    assert "monotonic" not in repr(span).lower()
    assert span["metadata"]["model"].startswith("meta_")
    assert "work-123" not in repr(span)
    assert "attempt-1" not in repr(span)
    assert "gpt-5" not in repr(span)


def test_recorder_rejects_url_shaped_correlation_identifiers():
    recorder = RuntimeSpanRecorder(work_id="https://private.example/work")
    handle = recorder.start(
        "operation",
        phase="overhead",
        parent_id="https://private.example/parent",
        metadata={"repository": "https://private.example/repository"},
    )
    recorder.finish(handle)

    span = recorder.export()[0]
    assert span["work_id"].startswith("wrk_")
    assert span["parent_id"].startswith("ref_")
    assert span["metadata"]["repository"].startswith("meta_")
    assert "private.example" not in repr(span)


def test_runtime_spans_never_retain_arbitrary_protected_strings():
    protected = [
        "/private/worktree/src/app.tsx",
        "[data-secret='selector']",
        "internal.example.test/api",
        "acme/private-repository",
        "/admin/settings",
        "api_key=not-a-real-key",
    ]
    recorder = RuntimeSpanRecorder(work_id=protected[0])
    handle = recorder.start(
        protected[1],
        phase=protected[2],
        attempt_id=protected[3],
        concurrency_id=protected[4],
        metadata={
            "operation": protected[1],
            "route": protected[4],
            "repository": protected[3],
            "check": protected[2],
            "source": protected[5],
            "selector": protected[1],
        },
    )
    recorder.finish(handle)

    serialized = repr(recorder.export())
    for value in protected:
        assert value not in serialized
    assert "api_key" not in serialized
    assert recorder.export()[0]["phase"] == "overhead"


def test_interval_summary_uses_union_not_summed_duration():
    spans = [
        {"phase": "tools", "started_at": 100.0, "ended_at": 105.0, "duration_s": 5.0},
        {"phase": "tools", "started_at": 102.0, "ended_at": 108.0, "duration_s": 6.0},
        {"phase": "model", "started_at": 108.0, "ended_at": 110.0, "duration_s": 2.0},
    ]

    summary = summarize_span_intervals(spans)

    assert summary["union_s"] == 10.0
    assert summary["summed_s"] == 13.0
    assert summary["overlap_s"] == 3.0
    assert summary["peak_concurrency"] == 2
    assert summary["phases"]["tools"] == {
        "union_s": 8.0,
        "summed_s": 11.0,
        "overlap_s": 3.0,
        "count": 2,
    }


def test_duplicate_span_ids_are_ignored_by_interval_summary():
    span = {
        "id": "span-1",
        "phase": "tools",
        "started_at": 1.0,
        "ended_at": 3.0,
        "duration_s": 2.0,
    }

    summary = summarize_span_intervals([span, dict(span)])

    assert summary["union_s"] == 2.0
    assert summary["count"] == 1
