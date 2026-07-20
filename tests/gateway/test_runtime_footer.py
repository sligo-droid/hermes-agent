"""Unit tests for gateway.runtime_footer — the opt-in runtime-metadata footer
appended to final gateway replies."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from gateway.runtime_footer import (
    _home_relative_cwd,
    _model_short,
    build_footer_line,
    format_runtime_footer,
    resolve_footer_config,
)


# ---------------------------------------------------------------------------
# _model_short + _home_relative_cwd
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "model,expected",
    [
        ("openai/gpt-5.4", "gpt-5.4"),
        ("anthropic/claude-sonnet-4.6", "claude-sonnet-4.6"),
        ("gpt-5.4", "gpt-5.4"),
        ("", ""),
        (None, ""),
    ],
)
def test_model_short_drops_vendor_prefix(model, expected):
    assert _model_short(model) == expected


def test_home_relative_cwd_collapses_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    sub = tmp_path / "projects" / "hermes"
    sub.mkdir(parents=True)
    result = _home_relative_cwd(str(sub))
    assert result == "~/projects/hermes"


def test_home_relative_cwd_leaves_abs_path_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "other"))
    result = _home_relative_cwd(str(tmp_path / "outside" / "dir"))
    assert result == str(tmp_path / "outside" / "dir")


def test_home_relative_cwd_empty_returns_empty():
    assert _home_relative_cwd("") == ""


# ---------------------------------------------------------------------------
# format_runtime_footer
# ---------------------------------------------------------------------------

def test_format_footer_all_fields(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path / "projects" / "hermes"))
    (tmp_path / "projects" / "hermes").mkdir(parents=True)
    out = format_runtime_footer(
        model="openrouter/openai/gpt-5.4",
        context_tokens=68000,
        context_length=100000,
        cwd=None,  # falls back to TERMINAL_CWD env var
        reasoning_effort="xhigh",
        fields=("model", "reasoning", "context_pct", "cwd"),
    )
    assert out == "gpt-5.4 · xhigh · 68% · ~/projects/hermes"


def test_format_footer_skips_missing_context_length():
    out = format_runtime_footer(
        model="openai/gpt-5.4",
        context_tokens=500,
        context_length=None,
        cwd="/tmp/wd",
        fields=("model", "context_pct", "cwd"),
    )
    # context_pct dropped silently; no "?%" artifact
    assert "%" not in out
    assert "gpt-5.4" in out
    assert "/tmp/wd" in out


def test_format_footer_context_pct_clamped_to_100():
    out = format_runtime_footer(
        model="m",
        context_tokens=500_000,  # way over
        context_length=100_000,
        cwd="",
        fields=("context_pct",),
    )
    assert out == "100%"


def test_format_footer_context_pct_never_negative():
    out = format_runtime_footer(
        model="m",
        context_tokens=-50,
        context_length=100,
        cwd="",
        fields=("context_pct",),
    )
    # Negative input => no field emitted (we require context_tokens >= 0)
    assert out == ""


def test_format_footer_empty_fields_returns_empty():
    out = format_runtime_footer(
        model="m", context_tokens=0, context_length=100,
        cwd="/x", fields=(),
    )
    assert out == ""


def test_format_footer_drops_cwd_when_empty(monkeypatch):
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    out = format_runtime_footer(
        model="openai/gpt-5.4",
        context_tokens=50, context_length=100,
        cwd="",
        fields=("model", "context_pct", "cwd"),
    )
    # cwd silently dropped; model + pct remain
    assert out == "gpt-5.4 · 50%"


def test_format_footer_custom_field_order():
    out = format_runtime_footer(
        model="openai/gpt-5.4",
        context_tokens=50, context_length=100,
        cwd="/opt/project",
        fields=("context_pct", "model"),  # swapped + no cwd
    )
    assert out == "50% · gpt-5.4"


def test_format_footer_unknown_field_silently_ignored():
    out = format_runtime_footer(
        model="openai/gpt-5.4",
        context_tokens=50, context_length=100,
        cwd="/x",
        fields=("model", "bogus", "context_pct"),
    )
    assert out == "gpt-5.4 · 50%"


def test_format_footer_includes_disabled_reasoning_level():
    out = format_runtime_footer(
        model="openai/gpt-5.6-luna",
        context_tokens=17,
        context_length=100,
        cwd="/tmp/workspace",
        reasoning_effort="none",
        fields=("model", "reasoning", "context_pct", "cwd"),
    )
    assert out == "gpt-5.6-luna · none · 17% · /tmp/workspace"


def test_format_footer_empty_workers_keeps_existing_output_byte_identical():
    out = format_runtime_footer(
        model="openai/gpt-5.6-sol",
        context_tokens=12,
        context_length=100,
        cwd="/tmp/workspace",
        reasoning_effort="xhigh",
        worker_runs=[],
    )
    assert out == "gpt-5.6-sol · xhigh · 12% · /tmp/workspace"


def test_format_footer_renders_one_worker():
    out = format_runtime_footer(
        model="",
        context_tokens=0,
        context_length=None,
        cwd="",
        worker_runs=[
            {
                "backend": "codex",
                "model": "openai/gpt-5.6-luna",
                "reasoning": "low",
            },
        ],
        fields=("workers",),
    )
    assert out == "workers: codex gpt-5.6-luna/low"


def test_format_footer_renders_workers_and_collapses_consecutive_duplicates():
    out = format_runtime_footer(
        model="",
        context_tokens=0,
        context_length=None,
        cwd="",
        worker_runs=[
            {"backend": "codex", "model": "gpt-5.6-luna", "reasoning": "low"},
            {"backend": "codex", "model": "gpt-5.6-luna", "reasoning": "low"},
            {
                "backend": "opencode",
                "model": "hermes-codex/gpt-5.6-sol",
                "reasoning": "xhigh",
            },
            {"backend": "codex", "model": "gpt-5.6-luna", "reasoning": "low"},
        ],
        fields=("workers",),
    )
    assert out == (
        "workers: codex gpt-5.6-luna/low x2, "
        "opencode gpt-5.6-sol/xhigh, codex gpt-5.6-luna/low"
    )


def test_format_footer_includes_general_delegate_model_and_reasoning():
    out = format_runtime_footer(
        model="",
        context_tokens=0,
        context_length=None,
        cwd="",
        worker_runs=[
            {"backend": "delegate", "model": "gpt-5.6-sol", "reasoning": "high"},
            {"backend": "delegate", "model": "gpt-5.6-terra", "reasoning": "xhigh"},
            {"backend": "codex", "model": "gpt-5.6-luna", "reasoning": "medium"},
            {"backend": "codex", "model": "gpt-5.6-luna", "reasoning": "medium"},
        ],
        fields=("workers",),
    )

    assert out == (
        "workers: delegate gpt-5.6-sol/high, delegate gpt-5.6-terra/xhigh, "
        "codex gpt-5.6-luna/medium x2"
    )


def test_format_footer_worker_omits_unknown_reasoning():
    out = format_runtime_footer(
        model="",
        context_tokens=0,
        context_length=None,
        cwd="",
        worker_runs=[{"backend": "claude_code", "model": "claude-fable-5"}],
        fields=("workers",),
    )
    assert out == "workers: claude_code claude-fable-5"


def test_format_footer_workers_respect_field_order():
    out = format_runtime_footer(
        model="openai/gpt-5.6-sol",
        context_tokens=0,
        context_length=None,
        cwd="",
        worker_runs=[
            {"backend": "codex", "model": "gpt-5.6-luna", "reasoning": "low"},
        ],
        fields=("workers", "model"),
    )
    assert out == "workers: codex gpt-5.6-luna/low · gpt-5.6-sol"


# ---------------------------------------------------------------------------
# resolve_footer_config
# ---------------------------------------------------------------------------

def test_resolve_defaults_off_empty_config():
    cfg = resolve_footer_config({}, "telegram")
    assert cfg == {
        "enabled": False,
        "fields": ["model", "reasoning", "context_pct", "cwd", "workers"],
    }


def test_resolve_global_enable():
    user = {"display": {"runtime_footer": {"enabled": True}}}
    cfg = resolve_footer_config(user, "telegram")
    assert cfg["enabled"] is True
    assert cfg["fields"] == ["model", "reasoning", "context_pct", "cwd", "workers"]


def test_resolve_platform_override_wins():
    user = {
        "display": {
            "runtime_footer": {"enabled": True, "fields": ["model"]},
            "platforms": {
                "slack": {"runtime_footer": {"enabled": False}},
            },
        },
    }
    # Telegram picks up the global enable
    assert resolve_footer_config(user, "telegram")["enabled"] is True
    # Slack overrides to off
    assert resolve_footer_config(user, "slack")["enabled"] is False


def test_resolve_platform_can_add_fields_only():
    user = {
        "display": {
            "runtime_footer": {"enabled": True},
            "platforms": {
                "discord": {"runtime_footer": {"fields": ["context_pct"]}},
            },
        },
    }
    tg = resolve_footer_config(user, "telegram")
    assert tg["enabled"] is True
    assert tg["fields"] == ["model", "reasoning", "context_pct", "cwd", "workers"]
    dc = resolve_footer_config(user, "discord")
    assert dc["enabled"] is True
    assert dc["fields"] == ["context_pct"]


def test_resolve_previous_default_field_lists_add_workers_immediately():
    user = {
        "display": {
            "runtime_footer": {
                "enabled": True,
                "fields": ["model", "reasoning", "context_pct", "cwd"],
            },
            "platforms": {
                "discord": {
                    "runtime_footer": {
                        "fields": ["model", "context_pct", "cwd"],
                    },
                },
            },
        },
    }

    assert resolve_footer_config(user, "telegram")["fields"] == [
        "model", "reasoning", "context_pct", "cwd", "workers",
    ]
    assert resolve_footer_config(user, "discord")["fields"] == [
        "model", "reasoning", "context_pct", "cwd", "workers",
    ]


def test_resolve_preserves_intentional_custom_fields():
    user = {
        "display": {
            "runtime_footer": {"enabled": True, "fields": ["model", "cwd"]},
        },
    }
    assert resolve_footer_config(user, "telegram")["fields"] == ["model", "cwd"]


def test_resolve_ignores_malformed_config():
    # Non-dict runtime_footer shouldn't crash
    user = {"display": {"runtime_footer": "on"}}
    cfg = resolve_footer_config(user, "telegram")
    assert cfg["enabled"] is False


# ---------------------------------------------------------------------------
# build_footer_line — top-level entry point used by gateway/run.py
# ---------------------------------------------------------------------------

def test_build_footer_empty_when_disabled():
    out = build_footer_line(
        user_config={},
        platform_key="telegram",
        model="openai/gpt-5.4",
        context_tokens=10, context_length=100,
        cwd="/tmp",
    )
    assert out == ""


def test_build_footer_returns_rendered_when_enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    out = build_footer_line(
        user_config={"display": {"runtime_footer": {"enabled": True}}},
        platform_key="telegram",
        model="openai/gpt-5.4",
        context_tokens=25, context_length=100,
        reasoning_effort="medium",
        cwd=str(tmp_path / "proj"),
        worker_runs=[
            {"backend": "codex", "model": "gpt-5.6-luna", "reasoning": "low"},
        ],
    )
    (tmp_path / "proj").mkdir(exist_ok=True)
    assert "gpt-5.4" in out
    assert "medium" in out
    assert "25%" in out
    assert "workers: codex gpt-5.6-luna/low" in out


def test_build_footer_per_platform_off_suppresses():
    user = {
        "display": {
            "runtime_footer": {"enabled": True},
            "platforms": {"slack": {"runtime_footer": {"enabled": False}}},
        },
    }
    out = build_footer_line(
        user_config=user,
        platform_key="slack",
        model="openai/gpt-5.4",
        context_tokens=10, context_length=100,
        cwd="/tmp",
    )
    assert out == ""


def test_gateway_footer_builder_passes_worker_runs_from_agent_result():
    from gateway.config import Platform
    from gateway.run import _runtime_footer_line_for_agent_result

    out = _runtime_footer_line_for_agent_result(
        user_config={
            "display": {
                "runtime_footer": {
                    "enabled": True,
                    "fields": ["workers"],
                },
            },
        },
        source=SimpleNamespace(platform=Platform.DISCORD),
        agent_result={
            "worker_runs": [
                {
                    "backend": "codex",
                    "model": "gpt-5.6-luna",
                    "reasoning": "low",
                },
            ],
        },
    )

    assert out == "workers: codex gpt-5.6-luna/low"


def test_build_footer_no_data_returns_empty_even_when_enabled():
    # Enabled, but context_length is None AND cwd empty AND model empty ⇒ no fields
    out = build_footer_line(
        user_config={"display": {"runtime_footer": {"enabled": True}}},
        platform_key="telegram",
        model="",
        context_tokens=0, context_length=None,
        cwd="",
    )
    # With no TERMINAL_CWD env either
    if not os.environ.get("TERMINAL_CWD"):
        assert out == ""
