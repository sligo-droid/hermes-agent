import os
from datetime import datetime, timedelta, timezone

from cron.jobs import save_job_output, update_job_output
from cron.scheduler import (
    _ingest_self_improvement_proposal_output,
    _render_job_output,
    _render_job_status_stub,
    reconcile_zero_byte_output_artifacts,
)


def _large_prompt(sentinel: str) -> str:
    lines = ["Injected prompt/context fixture"]
    lines.extend(f"context line {idx}" for idx in range(1, 180))
    lines.append(sentinel)
    return "\n".join(lines)


def _line_number(text: str, needle: str) -> int:
    for idx, line in enumerate(text.splitlines(), start=1):
        if needle in line:
            return idx
    raise AssertionError(f"{needle!r} not found")


def test_success_output_artifact_renders_final_response_before_large_prompt_context():
    prompt_sentinel = "UNIQUE_PROMPT_CONTEXT_SENTINEL_SUCCESS"
    final_response = "FINAL_RESPONSE_VISIBLE_NEAR_TOP"
    output = _render_job_output(
        {
            "id": "render-success-job",
            "name": "Render success job",
            "schedule_display": "every 1h",
        },
        _large_prompt(prompt_sentinel),
        status="success",
        final_response=final_response,
        run_time="2026-06-13 12:00:00",
    )

    assert output.startswith("# Cron Job: Render success job\n\n---\n")
    assert "artifact_schema: cron-output-v2" in output
    assert "rendering: final-first" in output
    assert 'job_id: "render-success-job"' in output
    assert 'job_name: "Render success job"' in output
    assert 'run_time: "2026-06-13 12:00:00"' in output
    assert 'schedule: "every 1h"' in output
    assert 'status: "success"' in output
    assert "**Job ID:** render-success-job" in output
    assert "**Run Time:** 2026-06-13 12:00:00" in output
    assert "**Schedule:** every 1h" in output
    assert "## Final response" in output
    assert "## Prompt/context transcript" in output
    assert "## Prompt" in output
    assert "## Response" in output

    final_line = _line_number(output, final_response)
    transcript_line = _line_number(output, "## Prompt/context transcript")
    prompt_line = _line_number(output, prompt_sentinel)
    raw_response_line = _line_number(output, "## Response")
    raw_response_content_line = max(
        idx
        for idx, line in enumerate(output.splitlines(), start=1)
        if final_response in line
    )
    assert final_line <= 50
    assert final_line < transcript_line < prompt_line
    assert prompt_line < raw_response_line < raw_response_content_line


def test_failure_output_artifact_renders_error_before_large_prompt_context():
    prompt_sentinel = "UNIQUE_PROMPT_CONTEXT_SENTINEL_FAILURE"
    error_text = "RuntimeError: boom near top"
    output = _render_job_output(
        {
            "id": "render-failure-job",
            "name": "Render failure job",
            "schedule_display": "daily at 02:00",
        },
        _large_prompt(prompt_sentinel),
        status="failed",
        error_text=error_text,
        run_time="2026-06-13 12:05:00",
    )

    assert output.startswith("# Cron Job: Render failure job (FAILED)\n\n---\n")
    assert "artifact_schema: cron-output-v2" in output
    assert "rendering: final-first" in output
    assert 'job_id: "render-failure-job"' in output
    assert 'job_name: "Render failure job"' in output
    assert 'run_time: "2026-06-13 12:05:00"' in output
    assert 'schedule: "daily at 02:00"' in output
    assert 'status: "failed"' in output
    assert "## Error" in output
    assert "## Prompt/context transcript" in output
    assert "## Prompt" in output
    assert "## Error detail" in output

    error_line = _line_number(output, error_text)
    transcript_line = _line_number(output, "## Prompt/context transcript")
    prompt_line = _line_number(output, prompt_sentinel)
    error_detail_line = _line_number(output, "## Error detail")
    assert error_line <= 50
    assert error_line < transcript_line < prompt_line
    assert prompt_line < error_detail_line


def test_failure_output_artifact_adds_codex_auth_incident_summary():
    output = _render_job_output(
        {
            "id": "codex-auth-job",
            "name": "Codex auth job",
            "schedule_display": "daily",
        },
        "prompt context",
        status="failed",
        error_text="RuntimeError: openai-codex upstream 401 token_invalidated access_token=secret-token",
        run_time="2026-06-13 12:06:00",
    )

    assert "### OpenAI Codex auth-route incident" in output
    assert "Provider route: `openai-codex`" in output
    assert "codex-auth-job (Codex auth job)" in output
    assert "access_token=<redacted>" in output
    assert "secret-token" not in output
    assert "RuntimeError: openai-codex upstream 401 token_invalidated access_token=<redacted>" in output


def test_saved_temp_output_artifact_keeps_final_first_rendering(tmp_path, monkeypatch):
    output = _render_job_output(
        {
            "id": "manual-inspection-job",
            "name": "Manual inspection job",
            "schedule_display": "manual",
        },
        _large_prompt("MANUAL_INSPECTION_PROMPT_SENTINEL"),
        status="success",
        final_response="MANUAL_INSPECTION_FINAL_RESPONSE",
        run_time="2026-06-13 12:10:00",
    )

    output_path = save_job_output("manual-inspection-job", output)
    saved = output_path.read_text(encoding="utf-8")

    assert output_path.name.endswith(".md")
    assert _line_number(saved, "MANUAL_INSPECTION_FINAL_RESPONSE") <= 50
    assert _line_number(saved, "MANUAL_INSPECTION_FINAL_RESPONSE") < _line_number(
        saved, "## Prompt/context transcript"
    )
    assert _line_number(saved, "## Prompt/context transcript") < _line_number(
        saved, "MANUAL_INSPECTION_PROMPT_SENTINEL"
    )
    assert _line_number(saved, "MANUAL_INSPECTION_PROMPT_SENTINEL") < _line_number(
        saved, "## Response"
    )
    assert saved.rfind("MANUAL_INSPECTION_FINAL_RESPONSE") > saved.find("## Response")


def test_reserved_output_artifact_is_non_empty_then_keeps_success_final_first(tmp_path, monkeypatch):
    job = {"id": "reserved-success-job", "name": "Reserved success", "schedule_display": "manual"}
    reserved = _render_job_status_stub(job, status="running", run_time="2026-06-13 12:20:00")

    output_path = save_job_output(job["id"], reserved)
    assert output_path.read_text(encoding="utf-8").strip()
    assert "artifact_schema: cron-output-status-v1" in output_path.read_text(encoding="utf-8")
    assert 'status: "running"' in output_path.read_text(encoding="utf-8")

    final = _render_job_output(
        job,
        _large_prompt("RESERVED_SUCCESS_PROMPT_SENTINEL"),
        status="success",
        final_response="RESERVED_SUCCESS_FINAL_RESPONSE",
        run_time="2026-06-13 12:21:00",
    )
    update_job_output(output_path, final)
    saved = output_path.read_text(encoding="utf-8")

    assert "artifact_schema: cron-output-v2" in saved
    assert "artifact_schema: cron-output-status-v1" not in saved
    assert _line_number(saved, "RESERVED_SUCCESS_FINAL_RESPONSE") <= 50
    assert _line_number(saved, "RESERVED_SUCCESS_FINAL_RESPONSE") < _line_number(
        saved, "## Prompt/context transcript"
    )


def test_status_stub_records_timeout_error_class_and_is_non_empty():
    output = _render_job_status_stub(
        {"id": "timeout-job", "name": "Timeout job", "schedule_display": "manual"},
        status="timed_out",
        run_time="2026-06-13 12:22:00",
        session_id="cron_timeout-job_20260613_122200",
        error_class="TimeoutError",
        message="Cron job timed out during inactivity closeout.",
    )

    assert output.strip()
    assert "artifact_schema: cron-output-status-v1" in output
    assert 'status: "timed_out"' in output
    assert 'session_id: "cron_timeout-job_20260613_122200"' in output
    assert 'error_class: "TimeoutError"' in output


def test_status_stub_records_interrupted_error_class_and_is_non_empty():
    output = _render_job_status_stub(
        {"id": "interrupted-job", "name": "Interrupted job", "schedule_display": "manual"},
        status="interrupted",
        run_time="2026-06-13 12:22:30",
        session_id="cron_interrupted-job_20260613_122230",
        error_class="InterruptedError",
        message="Cron job was interrupted before final output closeout.",
    )

    assert output.strip()
    assert "artifact_schema: cron-output-status-v1" in output
    assert 'status: "interrupted"' in output
    assert 'session_id: "cron_interrupted-job_20260613_122230"' in output
    assert 'error_class: "InterruptedError"' in output


def test_status_stub_records_manual_run_id():
    output = _render_job_status_stub(
        {"id": "manual-job", "name": "Manual job", "schedule_display": "manual"},
        status="running",
        run_time="2026-06-13 12:23:00",
        run_id="manual-run-123",
    )

    assert 'run_id: "manual-run-123"' in output


def test_self_improvement_ingestion_skips_silent_and_status_stub(monkeypatch, tmp_path):
    job = {
        "id": "proposal-silent-job",
        "name": "Proposal silent",
        "self_improvement_proposal": {"project": "pid", "prong": "p"},
    }
    monkeypatch.setattr("cron.scheduler._self_improvement_proposal_config", lambda _job: ("pid", "p"))

    def fail_ingest(*_args, **_kwargs):
        raise AssertionError("silent/status output should not be ingested")

    import self_improvement.proposal_storage as proposal_storage

    monkeypatch.setattr(proposal_storage, "ingest_proposal_output", fail_ingest)

    assert _ingest_self_improvement_proposal_output(
        job,
        "# Cron Job\n\nartifact_schema: cron-output-status-v1\n",
        tmp_path / "out.md",
        "",
    ) is None
    assert _ingest_self_improvement_proposal_output(
        job,
        "# Cron Job\n\nintentional silence",
        tmp_path / "out.md",
        "[SILENT]",
    ) is None


def test_reconcile_zero_byte_output_artifacts_annotates_eligible_manual_run(monkeypatch, tmp_path):
    import cron.scheduler as scheduler

    now = datetime(2026, 6, 18, 10, 0, tzinfo=timezone.utc)
    output_root = tmp_path / "output"
    artifact = output_root / "known-job" / "2026-06-18_09-00-00.md"
    artifact.parent.mkdir(parents=True)
    artifact.touch()
    old_mtime = (now - timedelta(minutes=45)).timestamp()
    artifact.touch()
    os.utime(artifact, (old_mtime, old_mtime))
    job = {
        "id": "known-job",
        "name": "Known job",
        "schedule_display": "manual",
        "manual_run": {"run_id": "run-123", "state": "running", "output_path": str(artifact)},
    }

    monkeypatch.setattr(scheduler, "load_jobs", lambda: [job])
    monkeypatch.setattr(
        scheduler,
        "_session_evidence_for_output_artifact",
        lambda job_id, artifact_time: {
            "available": True,
            "session_id": f"cron_{job_id}_20260618_090000",
            "ended_at": None,
        },
    )

    assert reconcile_zero_byte_output_artifacts(now=now, output_root=output_root, stale_after_seconds=1800) == 1
    saved = artifact.read_text(encoding="utf-8")
    assert saved.strip()
    assert "artifact_schema: cron-output-status-v1" in saved
    assert 'status: "running"' in saved
    assert 'job_id: "known-job"' in saved
    assert 'run_id: "run-123"' in saved
    assert 'session_id: "cron_known-job_20260618_090000"' in saved


def test_reconcile_zero_byte_output_artifacts_preserves_interrupted_manual_run_status(monkeypatch, tmp_path):
    import cron.scheduler as scheduler

    now = datetime(2026, 6, 18, 10, 0, tzinfo=timezone.utc)
    output_root = tmp_path / "output"
    artifact = output_root / "interrupted-job" / "2026-06-18_09-00-00.md"
    artifact.parent.mkdir(parents=True)
    artifact.touch()
    old_mtime = (now - timedelta(minutes=45)).timestamp()
    os.utime(artifact, (old_mtime, old_mtime))
    job = {
        "id": "interrupted-job",
        "name": "Interrupted job",
        "schedule_display": "manual",
        "manual_run": {
            "run_id": "run-interrupted",
            "state": "interrupted",
            "output_path": str(artifact),
            "error": "Manual cron run was interrupted before completion or lost during restart.",
        },
    }

    monkeypatch.setattr(scheduler, "load_jobs", lambda: [job])
    monkeypatch.setattr(
        scheduler,
        "_session_evidence_for_output_artifact",
        lambda job_id, artifact_time: {
            "available": True,
            "session_id": f"cron_{job_id}_20260618_090000",
            "ended_at": None,
        },
    )

    assert reconcile_zero_byte_output_artifacts(now=now, output_root=output_root, stale_after_seconds=1800) == 1
    saved = artifact.read_text(encoding="utf-8")
    assert saved.strip()
    assert "artifact_schema: cron-output-status-v1" in saved
    assert 'status: "interrupted"' in saved
    assert 'error_class: "InterruptedError"' in saved
    assert 'run_id: "run-interrupted"' in saved
    assert 'session_id: "cron_interrupted-job_20260618_090000"' in saved


def test_reconcile_zero_byte_output_artifacts_leaves_ineligible_files_unchanged(monkeypatch, tmp_path):
    import cron.scheduler as scheduler

    now = datetime(2026, 6, 18, 10, 0, tzinfo=timezone.utc)
    output_root = tmp_path / "output"
    fresh = output_root / "known-job" / "2026-06-18_09-50-00.md"
    historical = output_root / "known-job" / "2026-06-18_08-00-00.md"
    non_empty = output_root / "known-job" / "2026-06-18_07-00-00.md"
    non_cron = output_root / "unknown-job" / "2026-06-18_08-30-00.md"
    for path in (fresh, historical, non_empty, non_cron):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    non_empty.write_text("already has output", encoding="utf-8")
    old_mtime = (now - timedelta(hours=2)).timestamp()
    for path in (historical, non_empty, non_cron):
        os.utime(path, (old_mtime, old_mtime))
    fresh_mtime = (now - timedelta(minutes=5)).timestamp()
    os.utime(fresh, (fresh_mtime, fresh_mtime))

    monkeypatch.setattr(
        scheduler,
        "load_jobs",
        lambda: [{"id": "known-job", "name": "Known job", "schedule_display": "manual"}],
    )
    monkeypatch.setattr(
        scheduler,
        "_session_evidence_for_output_artifact",
        lambda *_args: {"available": True, "session_id": None, "ended_at": None},
    )

    assert reconcile_zero_byte_output_artifacts(now=now, output_root=output_root, stale_after_seconds=1800) == 0
    assert fresh.stat().st_size == 0
    assert historical.stat().st_size == 0
    assert non_cron.stat().st_size == 0
    assert non_empty.read_text(encoding="utf-8") == "already has output"


def test_reconcile_zero_byte_output_artifacts_rejects_distant_session_evidence(tmp_path):
    from hermes_state import SessionDB
    import cron.scheduler as scheduler

    now = datetime(2026, 6, 18, 10, 0, tzinfo=timezone.utc)
    home = tmp_path / "home"
    home.mkdir()
    db = SessionDB(db_path=home / "state.db")
    db.create_session("cron_known-job_20260618_060000", "cron")
    db._conn.execute(
        "UPDATE sessions SET started_at = ? WHERE id = ?",
        ((now - timedelta(hours=4)).timestamp(), "cron_known-job_20260618_060000"),
    )
    db._conn.commit()

    scheduler._hermes_home = home
    try:
        evidence = scheduler._session_evidence_for_output_artifact(
            "known-job",
            datetime(2026, 6, 18, 9, 0, tzinfo=timezone.utc),
        )
    finally:
        scheduler._hermes_home = None

    assert evidence["available"] is True
    assert evidence["session_id"] is None
