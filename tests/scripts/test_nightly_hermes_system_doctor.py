import importlib.util
import json
import os
import shutil
import sys

from scripts import nightly_hermes_system_doctor as doctor


def test_extract_compression_routes_accepts_warning_prefixed_stdout():
    output = (
        "resolve_provider_client: anthropic requested but no Anthropic credentials found\n"
        + json.dumps(
            {
                "routes": [
                    {
                        "label": "primary",
                        "provider": "anthropic",
                        "model": "claude-sonnet-4.6",
                        "exit": 1,
                        "error": "RuntimeError: Anthropic credentials unavailable",
                    },
                    {
                        "label": "fallback_chain[0]",
                        "provider": "openai-codex",
                        "model": "gpt-5.4-mini",
                        "exit": 0,
                        "output": "HERMES_COMPRESSION_ROUTE_OK_1",
                    },
                ]
            },
            sort_keys=True,
        )
        + "\n"
    )

    routes = doctor.extract_compression_routes(output)

    assert routes == [
        {
            "label": "primary",
            "provider": "anthropic",
            "model": "claude-sonnet-4.6",
            "exit": 1,
            "error": "RuntimeError: Anthropic credentials unavailable",
        },
        {
            "label": "fallback_chain[0]",
            "provider": "openai-codex",
            "model": "gpt-5.4-mini",
            "exit": 0,
            "output": "HERMES_COMPRESSION_ROUTE_OK_1",
        },
    ]


def test_record_compression_route_results_reports_failing_routes_only():
    output = (
        "resolve_provider_client: anthropic requested but no Anthropic credentials found\n"
        + json.dumps(
            {
                "routes": [
                    {
                        "label": "primary",
                        "provider": "anthropic",
                        "model": "claude-sonnet-4.6",
                        "exit": 1,
                        "error": "api_key=sk-secret token=secret-token",
                    },
                    {
                        "label": "fallback_chain[0]",
                        "provider": "openai-codex",
                        "model": "gpt-5.4-mini",
                        "exit": 0,
                        "output": "HERMES_COMPRESSION_ROUTE_OK_1",
                    },
                ]
            },
            sort_keys=True,
        )
    )
    facts = {}
    issues = []

    results = doctor.check_compression_routes(
        {"exit": 1, "output": output},
        issues,
        facts,
    )

    assert facts["compression_routes_output"] == output
    assert facts["compression_route_results"] == results
    assert results[1]["output"] == "HERMES_COMPRESSION_ROUTE_OK_1"
    assert [issue["name"] for issue in issues] == [
        "compression configured provider route failed: primary anthropic/claude-sonnet-4.6"
    ]
    assert "compression configured provider routes missing" not in issues[0]["name"]
    assert "sk-secret" not in issues[0]["detail"]
    assert "secret-token" not in issues[0]["detail"]
    assert "api_key=<redacted>" in issues[0]["detail"]
    assert "token=<redacted>" in issues[0]["detail"]


def test_record_compression_route_results_reports_missing_only_without_routes():
    facts = {}
    issues = []

    results = doctor.record_compression_route_results(
        {"exit": 1, "output": "provider warning\n{\"not_routes\": []}\n"},
        issues,
        facts,
    )

    assert results == []
    assert facts["compression_route_results"] == []
    assert [issue["name"] for issue in issues] == [
        "compression configured provider routes missing"
    ]


def test_record_honcho_watchdog_result_reports_disabled_repair_as_skipped():
    facts = {}
    issues = []
    output = """Honcho health alert: deterministic watchdog found unexpected output, but repair inference failed.
Inference error: RuntimeError: inference disabled by HONCHO_WATCHDOG_NO_INFERENCE=1

--- Raw watchdog facts ---
{
  "anomalies": [
    "queue errors: 4 queue errors since 2026-06-14T09:00:34.443950Z"
  ],
  "checked_at": "2026-06-15T09:00:19.779625Z"
}
"""

    doctor.record_honcho_watchdog_result(
        {"exit": 0, "output": output},
        issues,
        facts,
    )

    assert facts["honcho_watchdog_exit"] == 0
    assert facts["honcho_watchdog_output"] == output
    assert [issue["name"] for issue in issues] == [
        "honcho deterministic watchdog reported anomalies; repair inference skipped"
    ]
    assert "queue errors: 4 queue errors since 2026-06-14T09:00:34.443950Z" in issues[0]["detail"]
    assert "repair inference failed" in issues[0]["detail"]


def test_record_honcho_watchdog_result_reports_real_failures():
    facts = {}
    issues = []

    doctor.record_honcho_watchdog_result(
        {"exit": 1, "output": "Honcho watchdog crashed: RuntimeError: boom"},
        issues,
        facts,
    )

    assert [issue["name"] for issue in issues] == ["honcho deterministic watchdog failed"]


def test_record_honcho_watchdog_result_does_not_mask_failed_disabled_inference_output():
    facts = {}
    issues = []
    output = (
        "Honcho watchdog crashed before facts: "
        "RuntimeError: inference disabled by HONCHO_WATCHDOG_NO_INFERENCE=1"
    )

    doctor.record_honcho_watchdog_result(
        {"exit": 1, "output": output},
        issues,
        facts,
    )

    assert [issue["name"] for issue in issues] == ["honcho deterministic watchdog failed"]


def test_record_honcho_watchdog_result_accepts_ok_status():
    facts = {}
    issues = []

    doctor.record_honcho_watchdog_result(
        {"exit": 0, "output": "OK Honcho watchdog: services/endpoints passed"},
        issues,
        facts,
    )

    assert issues == []


def test_check_codex_auth_incidents_records_one_redacted_issue(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "logs").mkdir(parents=True)
    (home / "cron" / "output" / "job-4").mkdir(parents=True)
    (home / "logs" / "errors.log").write_text(
        "2026-06-15T03:00:00Z hermes_cli.proxy.adapters.openai_codex AuthenticationError 401 token_invalidated api_key=secret-key\n",
        encoding="utf-8",
    )
    (home / "cron" / "jobs.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": "job-4",
                        "name": "Proposal cron",
                        "last_status": "error",
                        "last_error": "openai-codex 401 token_invalidated refresh_token=secret-refresh",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (home / "cron" / "output" / "job-4" / "2026-06-15_03-00-00.md").write_text(
        "RuntimeError: OpenAI Codex upstream 401 token_invalidated access_token=secret-access\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(doctor, "HERMES_HOME", home)
    facts = {"hermes_auth_list": "openai-codex: logged in"}
    issues = []

    doctor.check_codex_auth_incidents(issues, facts)

    assert len(issues) == 1
    assert issues[0]["severity"] == "warning"
    assert issues[0]["name"] == "openai-codex credential invalidation incident detected"
    assert facts["codex_auth_incident"]["provider_route"] == "openai-codex"
    assert facts["codex_auth_incident"]["current_state"] == "recovered_logged_in"
    assert facts["codex_auth_incident"]["affected_cron_jobs"] == [{"id": "job-4", "name": "Proposal cron"}]
    detail = issues[0]["detail"]
    assert "secret-key" not in detail
    assert "secret-refresh" not in detail
    assert "secret-access" not in detail
    assert "Proposal cron" in detail


def test_repo_script_is_full_cron_entrypoint_for_live_cron_job():
    jobs_path = doctor.HERMES_HOME / "cron" / "jobs.json"
    if jobs_path.exists():
        jobs = json.loads(jobs_path.read_text()).get("jobs", [])
        cron_job = next(item for item in jobs if item.get("id") == "2ee992ee65f5")
        assert cron_job.get("script") == "nightly_hermes_system_doctor.py"

    assert callable(doctor.main)
    assert callable(doctor.check_hermes_doctor)
    assert callable(doctor.check_coding_worker)
    assert callable(doctor.install_live)


def test_live_cron_entrypoint_imports_and_runs_status_without_external_checks(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    hermes_home = tmp_path / "home"
    live = hermes_home / "scripts" / "nightly_hermes_system_doctor.py"
    jobs = hermes_home / "cron" / "jobs.json"
    state = hermes_home / "state" / "nightly-hermes-system-doctor.json"
    source = repo / "scripts" / "nightly_hermes_system_doctor.py"
    source.parent.mkdir(parents=True)
    live.parent.mkdir(parents=True)
    jobs.parent.mkdir(parents=True)
    shutil.copy2(doctor.Path(__file__).parents[2] / "scripts" / "nightly_hermes_system_doctor.py", source)
    shutil.copy2(source, live)
    jobs.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": "2ee992ee65f5",
                        "name": "Nightly Hermes system doctor",
                        "script": "nightly_hermes_system_doctor.py",
                        "schedule_display": "0 5 * * *",
                        "enabled": True,
                        "state": "scheduled",
                        "no_agent": False,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_REPO", str(repo))
    monkeypatch.setenv("HERMES_PYTHON", sys.executable)

    spec = importlib.util.spec_from_file_location("live_nightly_hermes_system_doctor", live)
    assert spec is not None
    live_doctor = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(live_doctor)

    def ok_check(_issues, facts):
        facts.setdefault("mock_checks", 0)
        facts["mock_checks"] += 1

    monkeypatch.setattr(live_doctor, "STATE", state)
    monkeypatch.setattr(live_doctor, "check_hermes_doctor", ok_check)
    monkeypatch.setattr(live_doctor, "check_auth_list", ok_check)
    monkeypatch.setattr(live_doctor, "check_codex_auth_incidents", ok_check)
    monkeypatch.setattr(live_doctor, "check_main_inference", ok_check)
    monkeypatch.setattr(live_doctor, "check_compression_inference", ok_check)
    monkeypatch.setattr(live_doctor, "check_honcho", ok_check)
    monkeypatch.setattr("sys.argv", [str(live), "--status", "--skip-coding-worker-smoke"])

    assert live_doctor.main() == 0
    status_output = capsys.readouterr().out
    assert "OK Nightly Hermes system doctor" in status_output
    facts = json.loads(state.read_text(encoding="utf-8"))["facts"]
    assert facts["cron_job_id"] == "2ee992ee65f5"
    assert facts["cron_job_script_resolved_path"] == str(live.resolve())
    assert facts["script_running_path"] == str(live.resolve())
    assert facts["script_pickup_ready"] is True


def test_script_provenance_records_source_and_live_match(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    hermes_home = tmp_path / "home"
    source = repo / "scripts" / "nightly_hermes_system_doctor.py"
    live = hermes_home / "scripts" / "nightly_hermes_system_doctor.py"
    jobs = hermes_home / "cron" / "jobs.json"
    source.parent.mkdir(parents=True)
    live.parent.mkdir(parents=True)
    jobs.parent.mkdir(parents=True)
    source.write_text("#!/usr/bin/env python3\nprint('doctor')\n")
    shutil.copy2(source, live)
    jobs.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": "2ee992ee65f5",
                        "name": "Nightly Hermes system doctor",
                        "script": "nightly_hermes_system_doctor.py",
                        "schedule_display": "0 5 * * *",
                        "enabled": True,
                        "state": "scheduled",
                        "no_agent": False,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(doctor, "REPO", repo)
    monkeypatch.setattr(doctor, "HERMES_HOME", hermes_home)
    monkeypatch.setattr(doctor, "REPO_SCRIPT", source)
    monkeypatch.setattr(doctor, "LIVE_SCRIPT", live)
    monkeypatch.setattr(doctor, "CRON_JOBS", jobs)

    facts = doctor.script_provenance()

    assert facts["script_live_path"] == str(live)
    assert facts["script_source_path"] == str(source)
    assert facts["script_live_sha256"] == facts["script_source_sha256"]
    assert facts["script_matches_source"] is True
    assert facts["script_entrypoint_source"] == doctor.ENTRYPOINT_SOURCE_LABEL
    assert facts["cron_job_id"] == "2ee992ee65f5"
    assert facts["cron_job_name"] == "Nightly Hermes system doctor"
    assert facts["cron_job_script"] == "nightly_hermes_system_doctor.py"
    assert facts["cron_job_script_resolved_path"] == str(live.resolve())
    assert facts["cron_job_schedule"] == "0 5 * * *"
    assert facts["cron_invokes_live_script"] is True
    assert facts["script_pickup_ready"] is True
    assert facts["script_pickup_required"] == "none; cron invokes the live script that matches the repo source"


def test_script_provenance_records_live_pickup_step_when_source_differs(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    hermes_home = tmp_path / "home"
    source = repo / "scripts" / "nightly_hermes_system_doctor.py"
    live = hermes_home / "scripts" / "nightly_hermes_system_doctor.py"
    jobs = hermes_home / "cron" / "jobs.json"
    source.parent.mkdir(parents=True)
    live.parent.mkdir(parents=True)
    jobs.parent.mkdir(parents=True)
    source.write_text("new doctor\n", encoding="utf-8")
    live.write_text("old doctor\n", encoding="utf-8")
    jobs.write_text(
        json.dumps({"jobs": [{"id": "2ee992ee65f5", "name": "Nightly Hermes system doctor", "script": "nightly_hermes_system_doctor.py"}]}),
        encoding="utf-8",
    )

    monkeypatch.setattr(doctor, "REPO", repo)
    monkeypatch.setattr(doctor, "HERMES_HOME", hermes_home)
    monkeypatch.setattr(doctor, "REPO_SCRIPT", source)
    monkeypatch.setattr(doctor, "LIVE_SCRIPT", live)
    monkeypatch.setattr(doctor, "CRON_JOBS", jobs)

    facts = doctor.script_provenance()

    assert facts["cron_invokes_live_script"] is True
    assert facts["script_matches_source"] is False
    assert facts["script_pickup_ready"] is False
    assert facts["script_pickup_required"] == f"run `{doctor.LIVE_INSTALL_COMMAND}` to copy the repo script to the live cron path"


def test_cron_entrypoint_provenance_records_mismatch(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    hermes_home = tmp_path / "home"
    source = repo / "scripts" / "nightly_hermes_system_doctor.py"
    live = hermes_home / "scripts" / "nightly_hermes_system_doctor.py"
    jobs = hermes_home / "cron" / "jobs.json"
    source.parent.mkdir(parents=True)
    live.parent.mkdir(parents=True)
    jobs.parent.mkdir(parents=True)
    source.write_text("doctor\n", encoding="utf-8")
    live.write_text("doctor\n", encoding="utf-8")
    jobs.write_text(
        json.dumps({"jobs": [{"id": "2ee992ee65f5", "name": "Nightly Hermes system doctor", "script": "/tmp/nightly_hermes_system_doctor.py"}]}),
        encoding="utf-8",
    )

    monkeypatch.setattr(doctor, "REPO", repo)
    monkeypatch.setattr(doctor, "HERMES_HOME", hermes_home)
    monkeypatch.setattr(doctor, "REPO_SCRIPT", source)
    monkeypatch.setattr(doctor, "LIVE_SCRIPT", live)
    monkeypatch.setattr(doctor, "CRON_JOBS", jobs)

    facts = doctor.script_provenance()

    assert facts["cron_job_script"] == "/tmp/nightly_hermes_system_doctor.py"
    assert facts["cron_job_script_resolved_path"] == "/tmp/nightly_hermes_system_doctor.py"
    assert facts["cron_invokes_live_script"] is False
    assert facts["script_pickup_ready"] is False
    assert facts["script_pickup_required"] == "update the Nightly Hermes system doctor cron script entrypoint to the live script path"


def test_install_live_backs_up_existing_script_and_preserves_executable(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    hermes_home = tmp_path / "home"
    source = repo / "scripts" / "nightly_hermes_system_doctor.py"
    live = hermes_home / "scripts" / "nightly_hermes_system_doctor.py"
    source.parent.mkdir(parents=True)
    live.parent.mkdir(parents=True)
    source.write_text("#!/usr/bin/env python3\nprint('new doctor')\n")
    source.chmod(0o755)
    live.write_text("#!/usr/bin/env python3\nprint('old doctor')\n")
    live.chmod(0o700)

    monkeypatch.setattr(doctor, "REPO_SCRIPT", source)
    monkeypatch.setattr(doctor, "LIVE_SCRIPT", live)
    monkeypatch.setattr(doctor, "HERMES_HOME", hermes_home)

    assert doctor.install_live() == 0

    backups = list((hermes_home / "scripts" / "archive").glob("nightly_hermes_system_doctor.py.*.bak"))
    assert len(backups) == 1
    assert backups[0].read_text() == "#!/usr/bin/env python3\nprint('old doctor')\n"
    assert live.read_text() == source.read_text()
    assert os.access(live, os.X_OK)
    assert doctor.sha256_file(live) == doctor.sha256_file(source)
    output = capsys.readouterr().out
    assert f"source={source}" in output
    assert f"destination={live}" in output
    assert "installed_matches_source=True" in output


def test_main_status_persists_provenance_and_normal_healthy_run_is_silent(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    hermes_home = tmp_path / "home"
    state = hermes_home / "state" / "nightly-hermes-system-doctor.json"
    source = repo / "scripts" / "nightly_hermes_system_doctor.py"
    live = hermes_home / "scripts" / "nightly_hermes_system_doctor.py"
    source.parent.mkdir(parents=True)
    live.parent.mkdir(parents=True)
    source.write_text("doctor source\n")
    shutil.copy2(source, live)

    def ok_check(_issues, facts):
        facts["mock_check_ran"] = True

    monkeypatch.setattr(doctor, "REPO", repo)
    monkeypatch.setattr(doctor, "HERMES_HOME", hermes_home)
    monkeypatch.setattr(doctor, "STATE", state)
    monkeypatch.setattr(doctor, "REPO_SCRIPT", source)
    monkeypatch.setattr(doctor, "LIVE_SCRIPT", live)
    monkeypatch.setattr(doctor, "check_hermes_doctor", ok_check)
    monkeypatch.setattr(doctor, "check_auth_list", ok_check)
    monkeypatch.setattr(doctor, "check_main_inference", ok_check)
    monkeypatch.setattr(doctor, "check_compression_inference", ok_check)
    monkeypatch.setattr(doctor, "check_honcho", ok_check)
    monkeypatch.setattr(doctor, "check_coding_worker", ok_check)

    monkeypatch.setattr("sys.argv", ["nightly_hermes_system_doctor.py", "--skip-coding-worker-smoke"])
    assert doctor.main() == 0
    assert capsys.readouterr().out == ""
    facts = json.loads(state.read_text())["facts"]
    assert facts["script_live_path"] == str(live)
    assert facts["script_source_path"] == str(source)
    assert facts["script_live_sha256"] == facts["script_source_sha256"]
    assert facts["script_matches_source"] is True

    monkeypatch.setattr("sys.argv", ["nightly_hermes_system_doctor.py", "--status", "--skip-opencode-smoke"])
    assert doctor.main() == 0
    status_output = capsys.readouterr().out
    assert "OK Nightly Hermes system doctor" in status_output
    assert f"source={doctor.ENTRYPOINT_SOURCE_LABEL}" in status_output
    assert "matches_source=True" in status_output
