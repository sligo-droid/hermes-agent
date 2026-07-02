from __future__ import annotations

import subprocess
from types import SimpleNamespace

from hermes_cli.fable_planner import (
    FABLE_MODEL,
    FablePlanRequest,
    build_fable_context_packet,
    fable_metadata,
    generate_fable_plan,
)


class FakeCompletions:
    def __init__(self):
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        message = SimpleNamespace(content="# Implementation Plan\n\nThis is a plan only; no implementation was performed.")
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")])


class FakeClient:
    def __init__(self):
        self.completions = FakeCompletions()
        self.chat = SimpleNamespace(completions=self.completions)


def test_generate_fable_plan_uses_anthropic_oauth_route(monkeypatch):
    client = FakeClient()
    calls = []

    def fake_resolve(provider, model):
        calls.append((provider, model))
        return client, model

    monkeypatch.setattr("agent.auxiliary_client.resolve_provider_client", fake_resolve)

    result = generate_fable_plan(FablePlanRequest(prompt="build a test plan"), config={})

    assert result.ok
    assert calls == [("anthropic", FABLE_MODEL)]
    assert client.completions.kwargs["model"] == FABLE_MODEL
    assert client.completions.kwargs["extra_body"]["reasoning"]["effort"] == "high"
    assert client.completions.kwargs["extra_body"]["reasoning"]["enabled"] is True


def test_generate_fable_plan_errors_when_route_unavailable(monkeypatch):
    monkeypatch.setattr("agent.auxiliary_client.resolve_provider_client", lambda *_args, **_kwargs: (None, None))

    result = generate_fable_plan(FablePlanRequest(prompt="plan x"), config={})

    assert not result.ok
    assert "Anthropic OAuth route" in result.error


def test_generate_fable_plan_does_not_shell_out_to_claude(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr("agent.auxiliary_client.resolve_provider_client", lambda provider, model: (client, model))

    def fail_run(*_args, **_kwargs):
        raise AssertionError("subprocess.run should not be used for the provider call")

    monkeypatch.setattr(subprocess, "run", fail_run)

    result = generate_fable_plan(FablePlanRequest(prompt="plan x"), config={})

    assert result.ok


def test_context_packet_contains_plan_only_contract():
    packet = build_fable_context_packet(FablePlanRequest(prompt="add feature", platform="discord", session_id="s1"))

    assert "add feature" in packet
    assert "Plan-Only Contract" in packet
    assert "Do not edit files" in packet
    assert "discord" in packet


def test_refusal_finish_reason_is_marked_refusal(monkeypatch):
    class RefusalCompletions:
        def create(self, **_kwargs):
            message = SimpleNamespace(content="I cannot help with that.")
            return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="refusal")])

    client = SimpleNamespace(chat=SimpleNamespace(completions=RefusalCompletions()))
    monkeypatch.setattr("agent.auxiliary_client.resolve_provider_client", lambda provider, model: (client, model))

    result = generate_fable_plan(FablePlanRequest(prompt="bad request"), config={})

    assert not result.ok
    assert result.refusal
    assert "refused" in result.error.lower()


def test_fable_config_fails_closed_for_wrong_model(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr("agent.auxiliary_client.resolve_provider_client", lambda provider, model: (client, model))

    result = generate_fable_plan(
        FablePlanRequest(prompt="plan x"),
        config={"fable": {"model": "claude-sonnet-4-6"}},
    )

    assert not result.ok
    assert "claude-fable-5" in result.error
    assert client.completions.kwargs is None


def test_default_config_pins_fable_route():
    from hermes_cli.config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["fable"]["provider"] == "anthropic"
    assert DEFAULT_CONFIG["fable"]["model"] == FABLE_MODEL
    assert DEFAULT_CONFIG["fable"]["route"] == "anthropic_oauth"


def test_fable_metadata_for_artifact():
    metadata = fable_metadata()

    assert metadata["command"] == "fable"
    assert metadata["plan_artifact_kind"] == "fable_plan"
    assert metadata["model"] == FABLE_MODEL
