from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

from agent import opencode_worker as ow


def _cfg(**coding_overrides):
    opencode_cfg = {"binary": "opencode"}
    coding_cfg = {"opencode": opencode_cfg}
    coding_cfg.update(coding_overrides)
    return {"coding_worker": coding_cfg}


def _option(cmd, name):
    return cmd[cmd.index(name) + 1] if name in cmd else None


def _process_result(stdout: str = "", stderr: str = "", returncode: int = 0, **overrides):
    data = {
        "returncode": returncode,
        "stdout": stdout,
        "stderr": stderr,
        "timed_out": False,
        "startup_timed_out": False,
        "duration_seconds": 0.1,
    }
    data.update(overrides)
    return ow._OpenCodeProcessResult(**data)


def test_simple_task_runs_build_only(monkeypatch, tmp_path):
    calls = []

    monkeypatch.setattr(ow.shutil, "which", lambda name: "/bin/opencode")

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return _process_result(
            stdout=json.dumps(
                {"type": "message", "sessionID": "ses-build", "message": "done"}
            )
            + "\n",
        )

    monkeypatch.setattr(ow, "_run_opencode_process", fake_run)

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
    assert "--pure" in calls[0]
    assert calls[0][3] == "Read the attached Hermes worker brief and follow it exactly."
    assert calls[0].index("--file") == len(calls[0]) - 2


def test_worker_brief_is_workspace_scoped_and_cleaned(monkeypatch, tmp_path):
    calls = []
    brief_paths = []

    monkeypatch.setattr(ow.shutil, "which", lambda name: "/bin/opencode")

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        brief_path = Path(_option(cmd, "--file"))
        brief_paths.append(brief_path)
        assert brief_path.is_relative_to(tmp_path)
        assert brief_path.parent == tmp_path / ".hermes-opencode"
        assert brief_path.exists()
        assert brief_path.read_text(encoding="utf-8").startswith("fix typo in README")
        return _process_result(
            stdout=json.dumps(
                {"type": "message", "sessionID": "ses-build", "message": "done"}
            )
            + "\n",
        )

    monkeypatch.setattr(ow, "_run_opencode_process", fake_run)

    result = ow.run_opencode_task(
        "fix typo in README",
        str(tmp_path),
        timeout=60,
        config=_cfg(),
    )

    assert result.error is None
    assert calls
    assert brief_paths
    assert not brief_paths[0].exists()


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
        return _process_result(
            stdout=json.dumps({"type": "message", "sessionID": sid, "message": text}) + "\n",
        )

    monkeypatch.setattr(ow, "_run_opencode_process", fake_run)

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
        return _process_result(
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
        )

    monkeypatch.setattr(ow, "_run_opencode_process", fake_run)

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


def test_sparse_json_output_recovers_final_text_from_export(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(ow.shutil, "which", lambda name: "/bin/opencode")

    def fake_process(cmd, **_kwargs):
        calls.append(cmd)
        return _process_result(
            stdout=json.dumps({"type": "step_start", "sessionID": "ses-export"}) + "\n",
        )

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        if cmd[1] == "export":
            return SimpleNamespace(
                returncode=0,
                stdout="Exporting session: ses-export\n"
                + json.dumps({
                    "messages": [
                        {"info": {"role": "user"}, "parts": [{"type": "text", "text": "brief"}]},
                        {
                            "info": {"role": "assistant"},
                            "parts": [
                                {"type": "text", "synthetic": True, "text": "tool chatter"},
                                {"type": "text", "text": "exported final"},
                            ],
                        },
                    ]
                })
                + "\n",
                stderr="",
            )

    monkeypatch.setattr(ow, "_run_opencode_process", fake_process)
    monkeypatch.setattr(ow.subprocess, "run", fake_run)

    result = ow.run_opencode_single_pass(
        "say done",
        str(tmp_path),
        timeout=60,
        agent="build",
        reasoning_level="xhigh",
        config=_cfg(),
    )

    assert result.error is None
    assert result.final_text == "exported final"
    assert result.thread_id == "ses-export"
    assert calls[1] == ["/bin/opencode", "export", "ses-export"]


def test_reasoning_levels_are_configurable_by_mode(monkeypatch, tmp_path):
    calls = []

    monkeypatch.setattr(ow.shutil, "which", lambda name: "/bin/opencode")

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return _process_result(
            stdout=json.dumps(
                {"type": "message", "sessionID": "ses-build", "message": "done"}
            )
            + "\n",
        )

    monkeypatch.setattr(ow, "_run_opencode_process", fake_run)

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
        return _process_result(
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
        )

    monkeypatch.setattr(ow, "_run_opencode_process", fake_run)

    result = ow.run_opencode_task(
        "fix parser.py",
        str(tmp_path),
        timeout=60,
        config=_cfg(),
    )

    assert result.error is not None
    assert "opencode auth login" in result.error
    assert result.thread_id == "ses-auth"


def test_context_overflow_is_not_classified_as_auth(monkeypatch, tmp_path):
    monkeypatch.setattr(ow.shutil, "which", lambda name: "/bin/opencode")

    def fake_run(cmd, **_kwargs):
        return _process_result(
            stdout=json.dumps(
                {
                    "type": "error",
                    "sessionID": "ses-context",
                    "error": {
                        "name": "ContextOverflowError",
                        "data": {"message": "Input exceeds context window of this model"},
                    },
                }
            )
            + "\n",
            stderr="OpenCode authentication failed from a previous summary\n" + ("x" * 10000),
        )

    monkeypatch.setattr(ow, "_run_opencode_process", fake_run)

    result = ow.run_opencode_task(
        "fix parser.py",
        str(tmp_path),
        timeout=60,
        config=_cfg(),
    )

    assert result.error is not None
    assert "context window exceeded" in result.error.lower()
    assert "opencode auth login" not in result.error
    assert "... [truncated]" in result.error
    assert len(result.error) < 4500
    assert result.thread_id == "ses-context"


def test_timeout_becomes_worker_error(monkeypatch, tmp_path):
    monkeypatch.setattr(ow.shutil, "which", lambda name: "/bin/opencode")

    def fake_run(cmd, **_kwargs):
        return _process_result(
            timed_out=True,
            startup_timed_out=True,
            duration_seconds=30.0,
        )

    monkeypatch.setattr(ow, "_run_opencode_process", fake_run)

    result = ow.run_opencode_task(
        "fix parser.py",
        str(tmp_path),
        timeout=30,
        config=_cfg(),
    )

    assert result.timed_out is True
    assert result.should_retire is True
    assert "produced no JSON events" in result.error


def test_startup_timeout_zero_is_preserved(monkeypatch):
    monkeypatch.delenv("HERMES_OPENCODE_STARTUP_TIMEOUT_SECONDS", raising=False)

    cfg = ow.load_opencode_config(_cfg(opencode={"startup_timeout_seconds": 0}))

    assert cfg["startup_timeout_seconds"] == 0


def test_startup_timeout_zero_is_passed_to_process(monkeypatch, tmp_path):
    calls = []

    monkeypatch.setattr(ow.shutil, "which", lambda name: "/bin/opencode")

    def fake_run(cmd, **kwargs):
        calls.append(kwargs)
        return _process_result(
            stdout=json.dumps(
                {"type": "message", "sessionID": "ses-build", "message": "done"}
            )
            + "\n",
        )

    monkeypatch.setattr(ow, "_run_opencode_process", fake_run)

    result = ow.run_opencode_task(
        "fix typo in README",
        str(tmp_path),
        timeout=60,
        config=_cfg(opencode={"startup_timeout_seconds": 0}),
    )

    assert result.error is None
    assert calls[0]["startup_timeout"] == 0


def test_process_startup_timeout_kills_no_output_child(tmp_path):
    result = ow._run_opencode_process(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        workdir=str(tmp_path),
        timeout=5,
        startup_timeout=0.2,
    )

    assert result.timed_out is True
    assert result.startup_timed_out is True
    assert result.duration_seconds < 2


def test_process_startup_timeout_zero_waits_for_turn_timeout(tmp_path):
    result = ow._run_opencode_process(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        workdir=str(tmp_path),
        timeout=0.3,
        startup_timeout=0,
    )

    assert result.timed_out is True
    assert result.startup_timed_out is False
    assert result.duration_seconds < 2


def test_process_timeout_preserves_partial_stdout(tmp_path):
    result = ow._run_opencode_process(
        [
            sys.executable,
            "-c",
            (
                "import json, time; "
                "print(json.dumps({'type':'message','message':'started'}), flush=True); "
                "time.sleep(5)"
            ),
        ],
        workdir=str(tmp_path),
        timeout=0.4,
        startup_timeout=2,
    )

    assert result.timed_out is True
    assert result.startup_timed_out is False
    assert '"started"' in result.stdout


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


def test_backend_ignores_removed_codex_worker_config_key(monkeypatch):
    monkeypatch.delenv("HERMES_CODING_WORKER_BACKEND", raising=False)
    cfg = {"codex_worker": {"backend": "opencode"}}

    assert ow.load_coding_worker_backend(cfg) == "codex"
