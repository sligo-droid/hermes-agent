from __future__ import annotations

from hermes_cli import codex_status
from hermes_cli.codex_status import build_codex_usage_report, format_codex_status_report


def test_build_codex_usage_report_reads_account_limits_without_starting_a_turn(monkeypatch):
    calls = []

    class FakeClient:
        def __init__(self, codex_bin="codex"):
            calls.append(("init", codex_bin))

        def initialize(self, **kwargs):
            calls.append(("initialize", kwargs["client_name"]))
            return {}

        def request(self, method, params=None, timeout=30.0):
            calls.append(("request", method, params))
            if method == "account/read":
                return {"account": {"type": "chatgpt", "planType": "pro"}}
            if method == "account/rateLimits/read":
                return {
                    "rateLimits": {
                        "limitId": "codex",
                        "primary": {"usedPercent": 1, "windowDurationMins": 300},
                        "secondary": {"usedPercent": 2, "windowDurationMins": 10080},
                        "planType": "pro",
                    }
                }
            raise AssertionError(f"unexpected codex method: {method}")

        def close(self):
            calls.append(("close",))

    monkeypatch.setattr(codex_status, "check_codex_binary", lambda _bin: (True, "0.130.0"))
    monkeypatch.setattr(codex_status, "CodexAppServerClient", FakeClient)

    report = build_codex_usage_report()

    methods = [call[1] for call in calls if call[0] == "request"]
    assert methods == ["account/read", "account/rateLimits/read"]
    assert "turn/start" not in methods
    assert "thread/start" not in methods
    assert "weekly: 2% used" in report


def test_format_codex_status_report_prioritizes_weekly_model_usage():
    report = format_codex_status_report(
        {"account": {"type": "chatgpt", "email": "user@example.test", "planType": "pro"}},
        {
            "rateLimits": {},
            "rateLimitsByLimitId": {
                "codex_bengalfox": {
                    "limitId": "codex_bengalfox",
                    "limitName": "GPT-5.3-Codex-Spark",
                    "primary": {
                        "usedPercent": 5,
                        "windowDurationMins": 300,
                        "resetsAt": 1779088461,
                    },
                    "secondary": {
                        "usedPercent": 14,
                        "windowDurationMins": 10080,
                        "resetsAt": 1779651129,
                    },
                    "credits": None,
                    "planType": "pro",
                },
                "codex": {
                    "limitId": "codex",
                    "limitName": None,
                    "primary": {"usedPercent": 13, "windowDurationMins": 300},
                    "secondary": {"usedPercent": 23, "windowDurationMins": 10080},
                    "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
                    "planType": "pro",
                },
            },
        },
    )

    assert "Codex Status" in report
    assert "Account: chatgpt, plan pro" in report
    assert "GPT-5.3-Codex-Spark (codex bengalfox)" in report
    assert "5h: 5% used" in report
    assert "weekly: 14% used" in report
    assert "Codex (codex)" in report
    assert "weekly: 23% used" in report
    assert "user@example.test" not in report


def test_format_codex_status_report_handles_missing_limits():
    report = format_codex_status_report({"account": {"type": "apiKey"}}, {})

    assert report == "Codex Status\n\nAccount: apiKey\nNo Codex rate-limit usage returned."
