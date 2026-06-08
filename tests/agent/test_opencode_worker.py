from __future__ import annotations

import io
import json
import sqlite3
import subprocess
import sys
import warnings
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


def _write_session_metadata(
    data_home: Path,
    *,
    session_id: str,
    title: str,
    directory: Path,
    agent: str = "build",
    model: str = '{"id":"gpt-5.5","providerID":"hermes-codex","variant":"xhigh"}',
    time_created: int = 1_700_000_000_000,
):
    db_path = data_home / "opencode" / "opencode.db"
    db_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE session (
                id text PRIMARY KEY,
                title text NOT NULL,
                directory text NOT NULL,
                agent text,
                model text,
                time_created integer NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO session (id, title, directory, agent, model, time_created)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (session_id, title, str(directory), agent, model, time_created),
        )
        conn.commit()
    finally:
        conn.close()


def test_simple_task_runs_build_only(monkeypatch, tmp_path):
    calls = []
    process_envs = []

    monkeypatch.setattr(ow.shutil, "which", lambda name: "/bin/opencode")

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        process_envs.append(kwargs.get("env"))
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
        "passes": [{"name": "build", "agent": "build", "reasoning": "medium"}],
    }
    assert _option(calls[0], "--agent") == "build"
    assert _option(calls[0], "--variant") == "medium"
    assert _option(calls[0], "--model") == "openai/gpt-5.5"
    assert "--pure" in calls[0]
    assert calls[0][3] == "Read the attached Hermes worker brief and follow it exactly."
    assert calls[0].index("--file") == len(calls[0]) - 2
    assert process_envs[0] is not None
    config_home = process_envs[0]["XDG_CONFIG_HOME"]
    assert not (ow.Path(config_home) / "opencode" / "opencode.json").exists()


def test_isolated_config_contains_direct_openai_model_without_mcps(monkeypatch, tmp_path):
    seen_config_home = None
    seen_payload = None

    monkeypatch.setattr(ow.shutil, "which", lambda name: "/bin/opencode")

    def fake_run(_cmd, **kwargs):
        nonlocal seen_config_home, seen_payload
        seen_config_home = ow.Path(kwargs["env"]["XDG_CONFIG_HOME"])
        config_path = seen_config_home / "opencode" / "opencode.json"
        seen_payload = json.loads(config_path.read_text(encoding="utf-8"))
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
    assert seen_payload == {
        "$schema": "https://opencode.ai/config.json",
        "plugin": [],
        "permission": "allow",
        "mcp": {},
        "model": "openai/gpt-5.5",
    }
    assert seen_config_home is not None
    assert not seen_config_home.exists()


def test_configured_hermes_codex_model_is_preserved():
    cfg = ow.load_opencode_config(_cfg(opencode={"model": "hermes-codex/gpt-5.5"}))

    assert cfg["model"] == "hermes-codex/gpt-5.5"


def test_hermes_codex_model_inlines_worker_brief(monkeypatch, tmp_path):
    calls = []
    seen_payload = None
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    config_dir = home / ".config" / "opencode"
    config_dir.mkdir(parents=True)
    (config_dir / "opencode.json").write_text(
        json.dumps(
            {
                "provider": {
                    "hermes-codex": {
                        "npm": "@ai-sdk/openai-compatible",
                        "name": "Hermes Codex Proxy",
                        "options": {"baseURL": "http://127.0.0.1:9999/v1"},
                        "models": {"gpt-5.5": {}},
                    }
                },
                "model": "hermes-codex/gpt-5.5",
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(ow.shutil, "which", lambda name: "/bin/opencode")

    def fake_run(cmd, **kwargs):
        nonlocal seen_payload
        calls.append(cmd)
        config_path = ow.Path(kwargs["env"]["XDG_CONFIG_HOME"]) / "opencode" / "opencode.json"
        seen_payload = json.loads(config_path.read_text(encoding="utf-8"))
        return _process_result(
            stdout=json.dumps(
                {"type": "message", "sessionID": "ses-build", "message": "done"}
            )
            + "\n",
        )

    monkeypatch.setattr(ow, "_run_opencode_process", fake_run)

    result = ow.run_opencode_task(
        "fix typo in README",
        str(workspace),
        timeout=60,
        config=_cfg(opencode={"model": "hermes-codex/gpt-5.5"}),
    )

    assert result.error is None
    assert _option(calls[0], "--model") == "hermes-codex/gpt-5.5"
    assert "--file" not in calls[0]
    assert "Hermes worker brief:\nfix typo in README" in calls[0][3]
    assert seen_payload["provider"]["hermes-codex"]["options"] == {
        "baseURL": "http://127.0.0.1:9999/v1"
    }
    assert seen_payload["mcp"] == {}
    assert not (workspace / ".hermes-opencode").exists()


def test_opencode_jsonc_user_config_allows_comments_and_trailing_commas(monkeypatch, tmp_path):
    home = tmp_path / "home"
    config_dir = home / ".config" / "opencode"
    config_dir.mkdir(parents=True)
    (config_dir / "opencode.jsonc").write_text(
        """
        {
          // normal JSONC line comment
          "provider": {
            "hermes-codex": {
              "npm": "@ai-sdk/openai-compatible",
              "options": {
                "baseURL": "http://127.0.0.1:9999/v1", // trailing field comment
                "literal": "literal,}",
              },
              "models": {
                "gpt-5.5": {},
              },
            },
          },
          /* block comments are accepted in opencode.jsonc */
          "model": "hermes-codex/gpt-5.5",
        }
        """,
        encoding="utf-8",
    )

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    assert ow._opencode_provider_config_for_model("hermes-codex/gpt-5.5") == {
        "npm": "@ai-sdk/openai-compatible",
        "options": {
            "baseURL": "http://127.0.0.1:9999/v1",
            "literal": "literal,}",
        },
        "models": {"gpt-5.5": {}},
    }


def test_invalid_opencode_json_warns_with_path_and_json_remains_strict(monkeypatch, tmp_path):
    home = tmp_path / "home"
    config_dir = home / ".config" / "opencode"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "opencode.json"
    config_path.write_text(
        '{"provider": {"hermes-codex": {"models": {"gpt-5.5": {},},},}}',
        encoding="utf-8",
    )

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert ow._read_opencode_user_config() == {}

    assert len(caught) == 1
    assert caught[0].category is RuntimeWarning
    message = str(caught[0].message)
    assert str(config_path) in message
    assert "Ignoring invalid OpenCode config" in message


def test_isolated_config_can_be_disabled(monkeypatch, tmp_path):
    process_envs = []

    monkeypatch.setattr(ow.shutil, "which", lambda name: "/bin/opencode")

    def fake_run(_cmd, **kwargs):
        process_envs.append(kwargs.get("env"))
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
        config=_cfg(opencode={"isolated_config": False}),
    )

    assert result.error is None
    assert process_envs == [None]


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


def test_worker_prompt_includes_dirty_repo_preflight(monkeypatch, tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, stdout=subprocess.DEVNULL)
    (tmp_path / "src.py").write_text("dirty = True\n", encoding="utf-8")
    seen_brief = ""

    monkeypatch.setattr(ow.shutil, "which", lambda name: "/bin/opencode")

    def fake_run(cmd, **_kwargs):
        nonlocal seen_brief
        brief_arg = _option(cmd, "--file")
        assert brief_arg is not None
        seen_brief = Path(brief_arg).read_text(encoding="utf-8")
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
    assert "Repository state preflight:" in seen_brief
    assert "dirty worktree" in seen_brief
    assert "?? src.py" in seen_brief
    assert "preserve unrelated changes" in seen_brief
    assert "fix typo in README" in seen_brief


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
    export_envs = []
    monkeypatch.setattr(ow.shutil, "which", lambda name: "/bin/opencode")

    def fake_process(cmd, **_kwargs):
        calls.append(cmd)
        return _process_result(
            stdout=json.dumps({"type": "step_start", "sessionID": "ses-export"}) + "\n",
        )

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        if cmd[1] == "export":
            export_envs.append(_kwargs.get("env"))
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
    export_calls = [cmd for cmd in calls if len(cmd) > 1 and cmd[1] == "export"]
    assert export_calls == [["/bin/opencode", "export", "ses-export"]]
    assert export_envs[0] is not None
    assert Path(export_envs[0]["XDG_CONFIG_HOME"]).name.startswith(
        "hermes-opencode-config-"
    )


def test_no_output_success_recovers_final_text_from_discovered_session(monkeypatch, tmp_path):
    calls = []
    data_home = tmp_path / "data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setattr(ow.shutil, "which", lambda name: "/bin/opencode")
    monkeypatch.setattr(ow.time, "time", lambda: 1_700_000_000.0)

    def fake_process(cmd, **kwargs):
        calls.append(cmd)
        title = _option(cmd, "--title")
        assert title is not None
        _write_session_metadata(
            data_home,
            session_id="ses-discovered",
            title=title,
            directory=tmp_path,
            time_created=1_700_000_000_100,
        )
        assert kwargs.get("env") is not None
        assert kwargs["env"]["XDG_DATA_HOME"] == str(data_home)
        return _process_result(stdout="", stderr="", returncode=0)

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        if cmd[1] == "export":
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "messages": [
                            {
                                "info": {"role": "assistant"},
                                "parts": [{"type": "text", "text": "recovered final"}],
                            }
                        ]
                    }
                ),
                stderr="",
            )
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(ow, "_run_opencode_process", fake_process)
    monkeypatch.setattr(ow.subprocess, "run", fake_run)

    result = ow.run_opencode_single_pass(
        "say done",
        str(tmp_path),
        timeout=60,
        agent="build",
        reasoning_level="xhigh",
        config=_cfg(opencode={"model": "hermes-codex/gpt-5.5"}),
    )

    assert result.error is None
    assert result.final_text == "recovered final"
    assert result.thread_id == "ses-discovered"
    export_calls = [cmd for cmd in calls if len(cmd) > 1 and cmd[1] == "export"]
    assert export_calls == [["/bin/opencode", "export", "ses-discovered"]]


def test_no_output_success_without_matching_session_does_not_export(monkeypatch, tmp_path):
    calls = []
    data_home = tmp_path / "data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setattr(ow.shutil, "which", lambda name: "/bin/opencode")
    monkeypatch.setattr(ow.time, "time", lambda: 1_700_000_000.0)
    _write_session_metadata(
        data_home,
        session_id="ses-other",
        title="unrelated title",
        directory=tmp_path,
        time_created=1_700_000_000_100,
    )

    def fake_process(cmd, **_kwargs):
        calls.append(cmd)
        return _process_result(stdout="", stderr="", returncode=0)

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        raise AssertionError(f"export should not be called: {cmd}")

    monkeypatch.setattr(ow, "_run_opencode_process", fake_process)
    monkeypatch.setattr(ow.subprocess, "run", fake_run)

    result = ow.run_opencode_single_pass(
        "say done",
        str(tmp_path),
        timeout=60,
        agent="build",
        reasoning_level="xhigh",
        config=_cfg(opencode={"model": "hermes-codex/gpt-5.5"}),
    )

    assert result.final_text == ""
    assert result.thread_id is None
    assert result.error == "OpenCode completed without producing final text."
    assert [cmd for cmd in calls if len(cmd) > 1 and cmd[1] == "export"] == []


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


def test_default_startup_watchdog_is_disabled(monkeypatch, tmp_path):
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
        config=_cfg(),
    )

    assert result.error is None
    assert calls[0]["startup_timeout"] == 0


def test_opencode_process_closes_stdin(monkeypatch, tmp_path):
    popen_kwargs = {}

    class FakeProcess:
        stdout = io.StringIO("")
        stderr = io.StringIO("")
        returncode = 0

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    def fake_popen(_cmd, **kwargs):
        popen_kwargs.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr(ow.subprocess, "Popen", fake_popen)

    result = ow._run_opencode_process(
        [sys.executable, "-c", "print('unused')"],
        workdir=str(tmp_path),
        timeout=5,
        startup_timeout=0,
    )

    assert result.returncode == 0
    assert popen_kwargs["stdin"] == ow.subprocess.DEVNULL


def test_process_wraps_opencode_run_in_gateway_child_scope(monkeypatch, tmp_path):
    captured = {}

    def fake_build(command, **kwargs):
        captured["command"] = list(command)
        captured["kwargs"] = kwargs
        from hermes_cli.gateway_child_isolation import GatewayChildScope

        return ["/usr/bin/systemd-run", "--user", "--scope", "--", *command], GatewayChildScope(
            enabled=True,
            unit="hermes-gateway-child-coding-worker-session-opencode-run.scope",
            kind="coding-worker",
            purpose="OpenCode coding worker build pass",
            command_label="opencode-run",
            workspace=str(tmp_path),
            session_key="discord:123",
        )

    class FakeProcess:
        stdout = io.StringIO("")
        stderr = io.StringIO("")
        returncode = 0

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["popen_kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(
        "hermes_cli.gateway_child_isolation.build_gateway_child_scope_argv",
        fake_build,
    )
    monkeypatch.setattr(ow.subprocess, "Popen", fake_popen)

    result = ow._run_opencode_process(
        ["opencode", "run", "brief"],
        workdir=str(tmp_path),
        timeout=5,
        startup_timeout=0,
        env={"HERMES_SESSION_KEY": "discord:123", "OPENAI_API_KEY": "secret"},
        scope_session_key="discord:123",
        scope_purpose="OpenCode coding worker build pass",
    )

    assert result.returncode == 0
    assert captured["command"] == ["opencode", "run", "brief"]
    assert captured["kwargs"]["kind"] == "coding-worker"
    assert captured["kwargs"]["purpose"] == "OpenCode coding worker build pass"
    assert captured["kwargs"]["command_label"] == "opencode-run"
    assert captured["kwargs"]["session_key"] == "discord:123"
    assert captured["kwargs"]["cwd"] == str(tmp_path)
    assert captured["kwargs"]["pipe_stdio"] is True
    assert captured["cmd"][:4] == ["/usr/bin/systemd-run", "--user", "--scope", "--"]
    assert captured["cmd"][-3:] == ["opencode", "run", "brief"]
    assert captured["popen_kwargs"]["cwd"] is None
    assert captured["popen_kwargs"]["stdin"] == ow.subprocess.DEVNULL


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
