from cron.jobs import save_job_output, update_job_output
from cron.scheduler import _ingest_self_improvement_proposal_output, _render_job_output, _render_job_status_stub


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
