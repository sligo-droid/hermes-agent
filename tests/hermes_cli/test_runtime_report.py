import json

from hermes_cli.runtime_report import parse_runtime_report_lines


def test_parse_runtime_report_separates_gateway_and_turn_timings():
    summary = {
        "session": "sess_1",
        "model": "codex-mini",
        "provider": "openai",
        "platform": "discord",
        "model_tier": "intermediate",
        "model_tier_source": "route",
        "runtime_route": "discord_action_request",
        "runtime_role": "interactive",
        "runtime_pass": "build",
        "reasoning_effort": "high",
        "reasoning_mode": "effort",
        "reasoning_enabled": True,
        "reasoning_source": "model_tier",
        "service_tier": "default",
        "service_tier_source": "gateway_disabled",
        "api_mode": "codex_responses",
        "exit_reason": "codex_app_server_completed",
        "response_len": 42,
        "total_ms": 12500,
        "api_ms": 11000,
        "tool_ms": 900,
        "overhead_ms": 600,
        "api_calls": 1,
        "tool_calls": 2,
        "top_tools": [
            {
                "name": "terminal",
                "count": 1,
                "duration_ms": 700,
                "errors": 0,
                "blocked": 0,
                "chars": 100,
            },
            {
                "name": "patch",
                "count": 1,
                "duration_ms": 200,
                "errors": 0,
                "blocked": 0,
                "chars": 50,
            },
        ],
    }
    lines = [
        "2026-05-28 10:00:00 INFO gateway.run: inbound message: "
        "platform=discord user=ben chat=123 msg='hi'",
        "2026-05-28 10:00:13 INFO gateway.run: response ready: "
        "platform=discord chat=123 time=13.2s api_calls=1 response=42 chars",
        "2026-05-28 10:00:13 INFO [sess_1] agent.conversation_loop: "
        f"turn_runtime_summary {json.dumps(summary, sort_keys=True)}",
    ]

    report = parse_runtime_report_lines(lines)

    assert report.gateway_flows == [
        {
            "session": None,
            "platform": "discord",
            "chat": "123",
            "gateway_wall_ms": 13200,
            "response_ready_api_calls": 1,
            "response_chars": 42,
        }
    ]
    assert report.turns == [
        {
            "session": "sess_1",
            "platform": "discord",
            "provider": "openai",
            "model": "codex-mini",
            "model_tier": "intermediate",
            "model_tier_source": "route",
            "runtime_route": "discord_action_request",
            "runtime_role": "interactive",
            "runtime_pass": "build",
            "reasoning_effort": "high",
            "reasoning_mode": "effort",
            "reasoning_enabled": True,
            "reasoning_source": "model_tier",
            "service_tier": "default",
            "service_tier_source": "gateway_disabled",
            "api_mode": "codex_responses",
            "exit_reason": "codex_app_server_completed",
            "turn_total_ms": 12500,
            "api_ms": 11000,
            "tool_ms": 900,
            "overhead_ms": 600,
            "api_calls": 1,
            "tool_calls": 2,
            "response_len": 42,
        }
    ]
    assert report.top_tools[0]["name"] == "terminal"
    assert report.top_tools[0]["duration_ms"] == 700


def test_parse_runtime_report_aggregates_top_tools_across_turns():
    def line(session: str, tool: str, ms: int) -> str:
        payload = {
            "session": session,
            "model": "m",
            "provider": "p",
            "total_ms": ms + 100,
            "api_ms": 50,
            "tool_ms": ms,
            "overhead_ms": 50,
            "api_calls": 1,
            "tool_calls": 1,
            "top_tools": [
                {
                    "name": tool,
                    "count": 1,
                    "duration_ms": ms,
                    "errors": 0,
                    "blocked": 0,
                    "chars": 10,
                }
            ],
        }
        return (
            f"2026-05-28 10:00:00 INFO [{session}] "
            "agent.conversation_loop: turn_runtime_summary "
            f"{json.dumps(payload)}"
        )

    report = parse_runtime_report_lines(
        [
            line("s1", "terminal", 300),
            line("s2", "terminal", 500),
            line("s3", "read_file", 600),
        ]
    )

    assert report.top_tools[:2] == [
        {
            "name": "terminal",
            "count": 2,
            "duration_ms": 800,
            "errors": 0,
            "blocked": 0,
            "chars": 20,
        },
        {
            "name": "read_file",
            "count": 1,
            "duration_ms": 600,
            "errors": 0,
            "blocked": 0,
            "chars": 10,
        },
    ]


def test_parse_runtime_report_ignores_malformed_summary_json():
    report = parse_runtime_report_lines(
        [
            "2026-05-28 10:00:00 INFO [s] agent.conversation_loop: "
            "turn_runtime_summary {not-json}"
        ]
    )

    assert report.turns == []
    assert report.gateway_flows == []
    assert report.top_tools == []
