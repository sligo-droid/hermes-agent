from hermes_cli.codex_auth_incidents import (
    classify_codex_auth_incident,
    collect_codex_auth_evidence,
    is_codex_auth_failure,
    redact,
    render_codex_auth_incident_summary,
    summarize_failure_text,
)


def test_classifies_codex_auth_burst_as_one_redacted_incident():
    evidence = [
        {
            "source": "/tmp/home/logs/errors.log",
            "text": "2026-06-15T01:02:03Z hermes_cli.proxy.adapters.openai_codex AuthenticationError 401 token_invalidated authorization=Bearer abc123 access_token=secret-access",
        },
        {
            "source": "/tmp/home/cron/output/job-1/2026-06-15_01-03-00.md",
            "text": "RuntimeError: openai-codex upstream 401 token_invalidated refresh_token=secret-refresh",
            "cron_job_id": "job-1",
            "cron_job_name": "Proposal cron",
        },
        {
            "source": "honcho.deriver",
            "text": "Honcho proxy 502 InternalServerError wrapping OpenAI Codex upstream 401 token_invalidated message body: private user prompt",
        },
        {
            "source": "agent.conversation_loop",
            "text": "AuthenticationError: unrelated anthropic 401 without requested route",
        },
    ]

    incident = classify_codex_auth_incident(evidence, auth_status_text="openai-codex: logged in")

    assert incident is not None
    assert incident.provider_route == "openai-codex"
    assert incident.incident_type == "credential_invalidation"
    assert incident.current_state == "recovered_logged_in"
    assert incident.historical_evidence_count == 3
    assert incident.first_seen == "2026-06-15T01:02:03Z"
    assert incident.affected_cron_jobs == [{"id": "job-1", "name": "Proposal cron"}]
    assert incident.affected_consumers == ["cron.scheduler", "honcho-proxy", "openai-codex-proxy"]

    summary = render_codex_auth_incident_summary(incident)
    assert "appears recovered/currently logged in" in summary
    assert "job-1 (Proposal cron)" in summary
    assert "honcho-proxy" in summary
    assert "secret-access" not in summary
    assert "secret-refresh" not in summary
    assert "private user prompt" not in summary


def test_current_auth_failure_status_is_distinguished_from_history():
    incident = classify_codex_auth_incident(
        [
            {
                "source": "cron.scheduler",
                "text": "RuntimeError: openai-codex AuthenticationError 401 token_invalidated",
                "cron_job_id": "job-2",
                "cron_job_name": "PID recommendations",
            }
        ],
        auth_status_text="openai-codex: auth failed 401 invalid",
    )

    assert incident is not None
    assert incident.current_state == "current_auth_failure"
    summary = render_codex_auth_incident_summary(incident)
    assert "currently failing auth/status checks" in summary


def test_redaction_covers_common_secret_shapes():
    raw = (
        "api_key=sk-test token=tok secret=s password=p Authorization: Bearer auth123 "
        "bearer plain123 {\"access_token\":\"a\",\"refresh_token\":\"r\"}"
    )

    safe = redact(raw)

    for leaked in ["sk-test", "auth123", "plain123", '"a"', '"r"']:
        assert leaked not in safe
    assert "api_key=<redacted>" in safe
    assert "bearer=<redacted>" in safe
    assert '"access_token":"<redacted>"' in safe
    assert '"refresh_token":"<redacted>"' in safe


def test_codex_route_matching_is_conservative():
    assert is_codex_auth_failure("openai-codex AuthenticationError 401 token_invalidated")
    assert is_codex_auth_failure("hermes_cli.proxy.adapters.openai_codex auth failed")
    assert not is_codex_auth_failure("anthropic AuthenticationError 401 token_invalidated")
    assert not is_codex_auth_failure("openai-codex rate limit exceeded")


def test_collects_bounded_evidence_from_synthetic_home(tmp_path):
    home = tmp_path / "home"
    (home / "logs").mkdir(parents=True)
    (home / "cron" / "output" / "job-3").mkdir(parents=True)
    (home / "logs" / "errors.log").write_text(
        "2026-06-15T02:00:00Z agent.conversation_loop openai-codex AuthenticationError token_invalidated access_token=secret\n",
        encoding="utf-8",
    )
    (home / "cron" / "jobs.json").write_text(
        '{"jobs":[{"id":"job-3","name":"Dogfood cron","last_status":"error","last_error":"openai-codex 401 token_invalidated"}]}',
        encoding="utf-8",
    )
    (home / "cron" / "output" / "job-3" / "2026-06-15_02-00-00.md").write_text(
        "RuntimeError: OpenAI Codex 401 token_invalidated refresh_token=secret\n",
        encoding="utf-8",
    )

    evidence = collect_codex_auth_evidence(home)
    incident = classify_codex_auth_incident(evidence, auth_status_text="openai-codex: logged in")

    assert incident is not None
    assert incident.historical_evidence_count == 3
    assert incident.affected_cron_jobs == [{"id": "job-3", "name": "Dogfood cron"}]


def test_unnamed_cron_jobs_do_not_use_prompt_as_incident_label(tmp_path):
    home = tmp_path / "home"
    (home / "cron" / "output" / "job-sensitive").mkdir(parents=True)
    sensitive_prompt = "Summarize confidential customer escalation with access_token=secret"
    (home / "cron" / "jobs.json").write_text(
        '{"jobs":[{"id":"job-sensitive","prompt":"'
        + sensitive_prompt
        + '","last_error":"openai-codex 401 token_invalidated"}]}',
        encoding="utf-8",
    )
    (home / "cron" / "output" / "job-sensitive" / "2026-06-15_02-00-00.md").write_text(
        "RuntimeError: OpenAI Codex 401 token_invalidated\n",
        encoding="utf-8",
    )

    evidence = collect_codex_auth_evidence(home)
    incident = classify_codex_auth_incident(evidence)
    summary = render_codex_auth_incident_summary(incident)

    assert incident is not None
    assert incident.affected_cron_jobs == [{"id": "job-sensitive", "name": "job-sensitive"}]
    assert "job-sensitive (job-sensitive)" in summary
    assert sensitive_prompt not in summary
    assert "confidential customer escalation" not in summary


def test_summarize_failure_text_uses_explicit_name_or_id_not_prompt():
    text = "RuntimeError: OpenAI Codex AuthenticationError 401 token_invalidated"
    sensitive_prompt = "Investigate private Discord message content"

    unnamed_summary = summarize_failure_text(text, job={"id": "job-unnamed", "prompt": sensitive_prompt})
    named_summary = summarize_failure_text(
        text,
        job={"id": "job-named", "name": "Nightly doctor", "prompt": sensitive_prompt},
    )

    assert unnamed_summary is not None
    assert "job-unnamed (job-unnamed)" in unnamed_summary
    assert sensitive_prompt not in unnamed_summary
    assert named_summary is not None
    assert "job-named (Nightly doctor)" in named_summary
    assert sensitive_prompt not in named_summary
