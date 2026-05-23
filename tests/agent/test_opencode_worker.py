from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

from agent import opencode_worker as ow


def _cfg(**coding_overrides):
    opencode_cfg = {"binary": "opencode"}
    coding_cfg = {"opencode": opencode_cfg}
    coding_cfg.update(coding_overrides)
    return {"coding_worker": coding_cfg}


def _option(cmd, name):
    return cmd[cmd.index(name) + 1] if name in cmd else None


def test_simple_task_runs_build_only(monkeypatch, tmp_path):
    calls = []

    monkeypatch.setattr(ow.shutil, "which", lambda name: "/bin/opencode")

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {"type": "message", "sessionID": "ses-build", "message": "done"}
            )
            + "\n",
            stderr="",
        )

    monkeypatch.setattr(ow.subprocess, "run", fake_run)

    result = ow.run_opencode_task(
        "fix typo in README",
        str(tmp_path),
        timeout=60,
        config=_cfg(),
    )

    assert result.error is None
    assert result.final_text == "done"
    assert result.agents == ["build"]
    assert result.run_profile == {
        "kind": "one_pass_simple_build",
        "label": "1-pass simple build",
        "pass_count": 1,
        "plan_used": False,
        "passes": [{"name": "build", "agent": "build", "reasoning": "xhigh"}],
    }
    assert _option(calls[0], "--agent") == "build"
    assert _option(calls[0], "--variant") == "xhigh"
    assert calls[0][2] == "Read the attached Hermes worker brief and follow it exactly."
    assert calls[0].index("--file") == len(calls[0]) - 2


def test_complex_task_runs_plan_then_build(monkeypatch, tmp_path):
    calls = []
    briefs = []

    monkeypatch.setattr(ow.shutil, "which", lambda name: "/bin/opencode")

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        brief = cmd[cmd.index("--file") + 1]
        briefs.append(open(brief, encoding="utf-8").read())
        agent = cmd[cmd.index("--agent") + 1]
        if agent == "plan":
            text = "Plan: update auth boundary, add regression tests."
            sid = "ses-plan"
        else:
            text = '{"status":"completed","summary":"built","changed_files":[],"tests":[]}'
            sid = "ses-build"
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"type": "message", "sessionID": sid, "message": text}) + "\n",
            stderr="",
        )

    monkeypatch.setattr(ow.subprocess, "run", fake_run)

    result = ow.run_opencode_task(
        "fix auth credential handling",
        str(tmp_path),
        timeout=60,
        config=_cfg(),
    )

    assert result.error is None
    assert result.agents == ["plan", "build"]
    assert result.plan_text.startswith("Plan:")
    assert result.run_profile == {
        "kind": "two_pass_plan_build",
        "label": "2-pass plan+build",
        "pass_count": 2,
        "plan_used": True,
        "passes": [
            {"name": "plan", "agent": "plan", "reasoning": "xhigh"},
            {"name": "build", "agent": "build", "reasoning": "medium"},
        ],
    }
    assert [_option(cmd, "--agent") for cmd in calls] == ["plan", "build"]
    assert [_option(cmd, "--variant") for cmd in calls] == ["xhigh", "medium"]
    assert "OpenCode plan to follow:" in briefs[1]


def test_nested_text_part_is_used_as_final_text(monkeypatch, tmp_path):
    monkeypatch.setattr(ow.shutil, "which", lambda name: "/bin/opencode")

    def fake_run(cmd, **_kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "type": "text",
                    "sessionID": "ses-plan",
                    "part": {
                        "type": "text",
                        "text": '{"status":"planned","tasks":[]}',
                    },
                }
            )
            + "\n",
            stderr="",
        )

    monkeypatch.setattr(ow.subprocess, "run", fake_run)

    result = ow.run_opencode_single_pass(
        "plan work",
        str(tmp_path),
        timeout=60,
        agent="plan",
        reasoning_level="xhigh",
        config=_cfg(),
    )

    assert result.error is None
    assert result.backend == "opencode"
    assert result.final_text == '{"status":"planned","tasks":[]}'
    assert result.thread_id == "ses-plan"
    assert result.run_profile == {
        "kind": "single_pass",
        "label": "1-pass plan",
        "pass_count": 1,
        "plan_used": False,
        "passes": [{"name": "plan", "agent": "plan", "reasoning": "xhigh"}],
    }


def test_reasoning_levels_are_configurable_by_mode(monkeypatch, tmp_path):
    calls = []

    monkeypatch.setattr(ow.shutil, "which", lambda name: "/bin/opencode")

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {"type": "message", "sessionID": "ses-build", "message": "done"}
            )
            + "\n",
            stderr="",
        )

    monkeypatch.setattr(ow.subprocess, "run", fake_run)

    cfg = _cfg(
        simple_build_reasoning_level="medium",
        complex_plan_reasoning_level="max",
        complex_build_reasoning_level="low",
    )

    simple = ow.run_opencode_task(
        "fix typo in README",
        str(tmp_path),
        timeout=60,
        config=cfg,
    )

    assert simple.error is None
    assert simple.run_profile["passes"] == [
        {"name": "build", "agent": "build", "reasoning": "medium"}
    ]
    assert [_option(cmd, "--agent") for cmd in calls] == ["build"]
    assert [_option(cmd, "--variant") for cmd in calls] == ["medium"]

    calls.clear()

    complex_result = ow.run_opencode_task(
        "fix production auth race",
        str(tmp_path),
        timeout=60,
        config=cfg,
    )

    assert complex_result.error is None
    assert complex_result.run_profile["passes"] == [
        {"name": "plan", "agent": "plan", "reasoning": "max"},
        {"name": "build", "agent": "build", "reasoning": "low"},
    ]
    assert [_option(cmd, "--agent") for cmd in calls] == ["plan", "build"]
    assert [_option(cmd, "--variant") for cmd in calls] == ["max", "low"]


def test_auth_error_is_classified(monkeypatch, tmp_path):
    monkeypatch.setattr(ow.shutil, "which", lambda name: "/bin/opencode")

    def fake_run(cmd, **_kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "type": "error",
                    "sessionID": "ses-auth",
                    "error": {
                        "name": "APIError",
                        "data": {
                            "message": "Your authentication token has been invalidated.",
                            "statusCode": 401,
                        },
                    },
                }
            )
            + "\n",
            stderr="",
        )

    monkeypatch.setattr(ow.subprocess, "run", fake_run)

    result = ow.run_opencode_task(
        "fix parser.py",
        str(tmp_path),
        timeout=60,
        config=_cfg(),
    )

    assert result.error is not None
    assert "opencode auth login" in result.error
    assert result.thread_id == "ses-auth"


def test_timeout_becomes_worker_error(monkeypatch, tmp_path):
    monkeypatch.setattr(ow.shutil, "which", lambda name: "/bin/opencode")

    def fake_run(cmd, **_kwargs):
        raise subprocess.TimeoutExpired(cmd, 30, output="", stderr="")

    monkeypatch.setattr(ow.subprocess, "run", fake_run)

    result = ow.run_opencode_task(
        "fix parser.py",
        str(tmp_path),
        timeout=30,
        config=_cfg(),
    )

    assert result.timed_out is True
    assert result.should_retire is True
    assert "timed out" in result.error


def test_missing_binary_becomes_worker_error(monkeypatch, tmp_path):
    monkeypatch.setattr(ow.shutil, "which", lambda name: None)

    result = ow.run_opencode_task(
        "fix parser.py",
        str(tmp_path),
        timeout=30,
        config=_cfg(),
    )

    assert result.error is not None
    assert "not found" in result.error.lower()


def test_backend_ignores_removed_codex_worker_config_key():
    cfg = {"codex_worker": {"backend": "opencode"}}

    assert ow.load_coding_worker_backend(cfg) == "codex"
