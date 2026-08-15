import importlib.util
import json
import os
import shutil
import subprocess
import sys

from scripts import nightly_hermes_system_doctor as doctor


def test_configured_inference_providers_collects_only_explicit_routes():
    config = {
        "model": {"provider": "cli-proxy-api", "default": "gpt-5.6-sol"},
        "fallback_model": {"provider": "cli-proxy-api", "model": "gpt-5.6-luna"},
        "auxiliary": {
            "compression": {
                "provider": "cli-proxy-api",
                "model": "claude-sonnet-4-6",
                "fallback_chain": [{"provider": "openai-codex", "model": "gpt-5.6-luna"}],
            },
            "title_generation": {"provider": "auto", "model": ""},
        },
    }

    assert doctor.configured_inference_providers(config) == {"cli-proxy-api", "openai-codex"}


def test_check_auth_list_ignores_unconfigured_unhealthy_provider(monkeypatch):
    monkeypatch.setattr(
        doctor,
        "run",
        lambda *args, **kwargs: {
            "exit": 0,
            "output": (
                "cli-proxy-api (1 credential):\n"
                "  #1 proxy api_key configured ←\n\n"
                "openai-codex (1 credential):\n"
                "  #1 legacy oauth exhausted (59m left)\n"
            ),
        },
    )
    facts = {}
    issues = []

    doctor.check_auth_list(issues, facts, {"cli-proxy-api"})

    assert issues == []
    assert facts["configured_inference_providers"] == ["cli-proxy-api"]


def test_check_auth_list_reports_unhealthy_configured_provider(monkeypatch):
    monkeypatch.setattr(
        doctor,
        "run",
        lambda *args, **kwargs: {
            "exit": 0,
            "output": "openai-codex (1 credential):\n  #1 active oauth auth failed (401)\n",
        },
    )
    facts = {}
    issues = []

    doctor.check_auth_list(issues, facts, {"openai-codex"})

    assert [issue["name"] for issue in issues] == [
        "configured provider credential pool has unhealthy credentials"
    ]


def test_check_main_inference_uses_configured_route(monkeypatch):
    captured = {}

    def fake_smoke(code, timeout=180):
        captured["code"] = code
        captured["timeout"] = timeout
        return {
            "exit": 0,
            "output": (
                '{"provider":"cli-proxy-api","model":"gpt-5.6-sol",'
                '"text":"HERMES_MAIN_SMOKE_OK"}'
            ),
        }

    monkeypatch.setattr(doctor, "python_smoke", fake_smoke)
    facts = {}
    issues = []

    doctor.check_main_inference(issues, facts)

    assert issues == []
    assert "load_config_readonly" in captured["code"]
    assert 'provider="openai-codex"' not in captured["code"]
    assert "strict_provider=True" in captured["code"]


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
    assert results[0]["failure_class"] == "route_failed"
    assert results[0]["fallback_status"] == "healthy"
    assert results[0]["impact"] == "configured primary route degraded; fallback chain is healthy"
    assert results[1]["output"] == "HERMES_COMPRESSION_ROUTE_OK_1"
    assert [issue["name"] for issue in issues] == [
        "compression configured provider route failed: primary anthropic/claude-sonnet-4.6"
    ]
    assert "compression configured provider routes missing" not in issues[0]["name"]
    assert "sk-secret" not in issues[0]["detail"]
    assert "secret-token" not in issues[0]["detail"]
    assert "api_key=<redacted>" in issues[0]["detail"]
    assert "token=<redacted>" in issues[0]["detail"]


def _record_routes(route_payload):
    facts = {}
    issues = []
    results = doctor.record_compression_route_results(
        {"exit": 1, "output": json.dumps({"routes": route_payload}, sort_keys=True)},
        issues,
        facts,
    )
    return results, issues, facts


def test_record_compression_route_results_classifies_exhausted_primary_with_healthy_fallback():
    results, issues, facts = _record_routes(
        [
            {
                "label": "primary",
                "provider": "anthropic",
                "model": "claude-sonnet-4.6",
                "exit": 1,
                "credential_status": "exhausted",
                "credential_last_error_code": 429,
                "credential_last_error_reason": "rate_limit",
                "credential_last_error_message": "token=secret-token resets in 4 min",
                "credential_last_error_reset_at": "2026-07-09T12:00:00+00:00",
                "error": "RuntimeError: Anthropic OAuth credential unavailable",
            },
            {
                "label": "fallback_chain[0]",
                "provider": "openai-codex",
                "model": "gpt-5.4-mini",
                "exit": 0,
                "output": "HERMES_COMPRESSION_ROUTE_OK_1",
            },
        ]
    )

    assert facts["compression_route_results"] == results
    assert results[0]["failure_class"] == "credential_rate_limited_or_quarantined"
    assert results[0]["fallback_status"] == "healthy"
    assert results[0]["fallback_chain_healthy"] is True
    assert results[0]["impact"] == "configured primary route degraded; fallback chain is healthy"
    assert "Wait for credential cooldown" in results[0]["next_action"]
    assert [issue["name"] for issue in issues] == [
        "compression configured provider route failed: primary anthropic/claude-sonnet-4.6"
    ]
    detail = issues[0]["detail"]
    assert "credential_rate_limited_or_quarantined" in detail
    assert "configured primary route degraded" in detail
    assert "secret-token" not in detail
    assert "token=<redacted>" in detail


def test_record_compression_route_results_classifies_missing_primary_with_healthy_fallback():
    results, issues, _facts = _record_routes(
        [
            {
                "label": "primary",
                "provider": "anthropic",
                "model": "claude-sonnet-4.6",
                "exit": 1,
                "credential_status": "missing",
                "error": "RuntimeError: Anthropic credentials unavailable",
            },
            {
                "label": "fallback_chain[0]",
                "provider": "openai-codex",
                "model": "gpt-5.4-mini",
                "exit": 0,
            },
        ]
    )

    assert results[0]["failure_class"] == "missing_credential"
    assert results[0]["fallback_status"] == "healthy"
    assert "Add or configure credentials for anthropic" in results[0]["next_action"]
    assert issues[0]["severity"] == "critical"


def test_record_compression_route_results_keeps_hard_critical_when_primary_and_fallback_fail():
    results, issues, _facts = _record_routes(
        [
            {
                "label": "primary",
                "provider": "anthropic",
                "model": "claude-sonnet-4.6",
                "exit": 1,
                "credential_status": "exhausted",
                "credential_cooldown_seconds_remaining": 240,
            },
            {
                "label": "fallback_chain[0]",
                "provider": "openai-codex",
                "model": "gpt-5.4-mini",
                "exit": 1,
                "credential_status": "available",
                "error": "RuntimeError: fallback failed",
            },
        ]
    )

    assert results[0]["fallback_status"] == "failed"
    assert results[0]["impact"] == "compression route unavailable; fallback chain is not healthy"
    assert len(issues) == 2
    assert all(issue["severity"] == "critical" for issue in issues)


def test_record_compression_route_results_primary_success_has_no_issue():
    results, issues, _facts = _record_routes(
        [
            {
                "label": "primary",
                "provider": "anthropic",
                "model": "claude-sonnet-4.6",
                "exit": 0,
                "credential_status": "available",
                "output": "HERMES_COMPRESSION_ROUTE_OK_0",
            },
            {
                "label": "fallback_chain[0]",
                "provider": "openai-codex",
                "model": "gpt-5.4-mini",
                "exit": 0,
            },
        ]
    )

    assert results[0]["failure_class"] == "healthy"
    assert results[0]["impact"] == "primary route healthy"
    assert issues == []


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


def test_classify_qmd_target_requires_process_and_port_ready():
    target = {"name": "qmd-8181", "host": "127.0.0.1", "port": 8181}

    result = doctor.classify_qmd_target(
        target,
        ["python -m qmd --host 127.0.0.1 --port 8181"],
        lambda _host, _port: (True, None),
    )

    assert result["status"] == "healthy"
    assert result["process_present"] is True
    assert result["port_ready"] is True


def test_classify_qmd_target_accepts_ipv6_loopback_when_ipv4_refuses():
    target = {"name": "qmd-8181", "host": "127.0.0.1", "port": 8181}

    def connector(host, _port):
        if host == "::1":
            return True, None
        return False, f"ConnectionRefusedError: refused on {host}"

    result = doctor.classify_qmd_target(
        target,
        ["/home/droid/.local/bin/qmd --index pid-docs --port 8181"],
        connector,
    )

    assert result["status"] == "healthy"
    assert result["port_ready"] is True
    assert result["reachable_host"] == "::1"
    assert "127.0.0.1" in result["loopback_errors"]


def test_classify_qmd_target_reports_process_present_port_closed():
    target = {"name": "qmd-8181", "host": "127.0.0.1", "port": 8181}

    result = doctor.classify_qmd_target(
        target,
        ["python -m qmd --host 127.0.0.1 --port 8181"],
        lambda _host, _port: (False, "ConnectionRefusedError: refused"),
    )

    assert result["status"] == "port_closed"
    assert result["process_present"] is True
    assert result["port_ready"] is False
    assert "refused" in result["port_error"]


def test_collect_qmd_process_commands_uses_wide_ps_and_preserves_late_qmd_args(monkeypatch):
    long_prefix = "x" * 500

    def fake_run(cmd, **kwargs):
        assert cmd == ["ps", "-ww", "-eo", "pid=,args="]
        assert kwargs["cwd"] == "/"
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=(
                f" 111 python unrelated {long_prefix}\n"
                f" 222 /usr/bin/python {long_prefix} /home/droid/.local/bin/qmd --index skills --port 8182\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(doctor.subprocess, "run", fake_run)

    commands = doctor.collect_qmd_process_commands()

    assert len(commands) == 1
    assert "/home/droid/.local/bin/qmd" in commands[0]
    assert "--port 8182" in commands[0]


def test_classify_qmd_target_reports_process_missing_even_if_port_open():
    target = {"name": "qmd-8182", "host": "127.0.0.1", "port": 8182}

    result = doctor.classify_qmd_target(
        target,
        ["python -m qmd --host 127.0.0.1 --port 8181"],
        lambda _host, _port: (True, None),
    )

    assert result["status"] == "process_missing"
    assert result["process_present"] is False
    assert result["port_ready"] is True


def test_classify_qmd_target_reports_wrong_bind_when_command_advertises_non_loopback_host():
    target = {"name": "qmd-8181", "host": "127.0.0.1", "port": 8181}

    result = doctor.classify_qmd_target(
        target,
        ["python -m qmd --host 0.0.0.0 --port 8181"],
        lambda _host, _port: (False, "ConnectionRefusedError: refused"),
    )

    assert result["status"] == "wrong_bind"
    assert result["process_present"] is True
    assert result["port_ready"] is False


def test_check_qmd_health_records_facts_and_issues_for_unhealthy_targets():
    facts = {}
    issues = []

    results = doctor.check_qmd_health(
        issues,
        facts,
        process_collector=lambda: ["python -m qmd --host 127.0.0.1 --port 8181"],
        connector=lambda _host, _port: (False, "ConnectionRefusedError: refused"),
    )

    assert [item["status"] for item in results] == ["port_closed", "process_missing"]
    assert facts["qmd_expected_target_source"] == "nightly doctor defaults: local ports 8181 and 8182"
    assert facts["qmd_health"] == results
    assert [issue["name"] for issue in issues] == [
        "QMD service readiness failed: qmd-8181 port_closed",
        "QMD service readiness failed: qmd-8182 process_missing",
    ]
    assert "do not infer QMD health from process presence alone" in issues[0]["detail"]


def test_check_xai_plugin_imports_records_helper_provenance(monkeypatch):
    facts = {}
    issues = []
    output = json.dumps(
        {
            "has_build_xai_storage_options": True,
            "helper_module_file": "/repo/tools/xai_http.py",
            "plugins.image_gen.xai": "/repo/plugins/image_gen/xai/__init__.py",
            "plugins.video_gen.xai": "/repo/plugins/video_gen/xai/__init__.py",
            "plugins.model-providers.xai": "/repo/plugins/model-providers/xai/__init__.py",
            "tools.xai_http": "/repo/tools/xai_http.py",
        },
        sort_keys=True,
    )

    monkeypatch.setattr(
        doctor,
        "python_smoke",
        lambda code, timeout=180: {"exit": 0, "output": output},
    )

    doctor.check_xai_plugin_imports(issues, facts)

    assert issues == []
    assert facts["xai_plugin_imports_exit"] == 0
    assert '"has_build_xai_storage_options": true' in facts["xai_plugin_imports_output"]


def test_check_xai_plugin_imports_reports_missing_storage_helper(monkeypatch):
    facts = {}
    issues = []

    monkeypatch.setattr(
        doctor,
        "python_smoke",
        lambda code, timeout=180: {
            "exit": 1,
            "output": "ImportError: cannot import name 'build_xai_storage_options' from 'tools.xai_http' (/stale/tools/xai_http.py)",
        },
    )

    doctor.check_xai_plugin_imports(issues, facts)

    assert facts["xai_plugin_imports_exit"] == 1
    assert [issue["name"] for issue in issues] == ["xAI optional plugin import smoke failed"]
    assert "build_xai_storage_options" in issues[0]["detail"]


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

    def ok_check(_issues, facts, *_args):
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

    def ok_check(_issues, facts, *_args):
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
