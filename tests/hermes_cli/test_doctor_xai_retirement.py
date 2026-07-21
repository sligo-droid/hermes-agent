"""Focused doctor coverage for the xAI model-retirement diagnostic."""

from __future__ import annotations

import hermes_cli.config as config_mod
from hermes_cli import doctor


def test_doctor_xai_retirement_clean_config_reports_ok(monkeypatch, capsys):
    monkeypatch.setattr(
        config_mod,
        "load_config",
        lambda: {"principal": {"provider": "xai", "model": "grok-4.3"}},
    )
    manual_issues: list[str] = []

    doctor._check_xai_model_retirement(manual_issues)

    output = capsys.readouterr().out
    assert "xAI Model Retirement (May 15, 2026)" in output
    assert "No retired xAI models in config" in output
    assert manual_issues == []


def test_doctor_xai_retirement_reports_replacement_and_manual_issue(
    monkeypatch,
    capsys,
):
    monkeypatch.setattr(
        config_mod,
        "load_config",
        lambda: {"principal": {"provider": "xai", "model": "grok-3"}},
    )
    manual_issues: list[str] = []

    doctor._check_xai_model_retirement(manual_issues)

    output = capsys.readouterr().out
    assert "principal.model: 'grok-3' → use 'grok-4.3'" in output
    assert "https://docs.x.ai/developers/migration/may-15-retirement" in output
    assert len(manual_issues) == 1
    assert "Update 1 retired xAI model reference(s)" in manual_issues[0]


def test_doctor_xai_retirement_failure_is_nonfatal_and_redacted(
    monkeypatch,
    capsys,
):
    secret = "xai-" + "A" * 30

    def fail_load_config():
        raise RuntimeError(f"XAI_API_KEY={secret}")

    monkeypatch.setattr(config_mod, "load_config", fail_load_config)
    manual_issues: list[str] = []

    doctor._check_xai_model_retirement(manual_issues)

    output = capsys.readouterr().out
    assert "xAI retirement check skipped" in output
    assert secret not in output
    assert "XAI_API_KEY=***" in output
    assert manual_issues == []
