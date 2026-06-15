import json

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
