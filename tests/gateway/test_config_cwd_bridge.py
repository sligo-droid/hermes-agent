"""Tests for the config.yaml → env var bridge logic in gateway/run.py.

Specifically tests that top-level `cwd:` and `backend:` in config.yaml
are correctly bridged to TERMINAL_CWD / TERMINAL_ENV env vars as
convenience aliases for `terminal.cwd` / `terminal.backend`.

The bridge logic is module-level code in gateway/run.py, exposed through
``_bridge_gateway_config_to_env`` so tests exercise the real behavior.
"""

import os
from pathlib import Path


def _simulate_config_bridge(
    cfg: dict,
    initial_env: dict | None = None,
    hermes_home: str = "/root/.hermes",
):
    """Run the real gateway config bridge helper against an isolated env."""
    from gateway.run import _bridge_gateway_config_to_env

    env = dict(initial_env or {})
    return _bridge_gateway_config_to_env(cfg, env, hermes_home)


class TestTopLevelCwdAlias:
    """Top-level `cwd:` should be treated as `terminal.cwd`."""

    def test_top_level_cwd_sets_terminal_cwd(self):
        cfg = {"cwd": "/home/hermes/projects"}
        result = _simulate_config_bridge(cfg)
        assert result["TERMINAL_CWD"] == "/home/hermes/projects"

    def test_top_level_backend_sets_terminal_env(self):
        cfg = {"backend": "docker"}
        result = _simulate_config_bridge(cfg)
        assert result["TERMINAL_ENV"] == "docker"

    def test_top_level_cwd_and_backend(self):
        cfg = {"backend": "local", "cwd": "/home/hermes/projects"}
        result = _simulate_config_bridge(cfg)
        assert result["TERMINAL_CWD"] == "/home/hermes/projects"
        assert result["TERMINAL_ENV"] == "local"

    def test_nested_terminal_takes_precedence_over_top_level(self):
        """terminal.cwd should win over top-level cwd."""
        cfg = {
            "cwd": "/should/not/use",
            "terminal": {"cwd": "/home/hermes/real"},
        }
        result = _simulate_config_bridge(cfg)
        assert result["TERMINAL_CWD"] == "/home/hermes/real"

    def test_nested_terminal_backend_takes_precedence(self):
        cfg = {
            "backend": "should-not-use",
            "terminal": {"backend": "docker"},
        }
        result = _simulate_config_bridge(cfg)
        assert result["TERMINAL_ENV"] == "docker"

    def test_terminal_env_type_legacy_backend_alias(self):
        cfg = {"terminal": {"env_type": "docker"}}
        result = _simulate_config_bridge(cfg)
        assert result["TERMINAL_ENV"] == "docker"

    def test_terminal_backend_takes_precedence_over_env_type(self):
        cfg = {"terminal": {"backend": "local", "env_type": "docker"}}
        result = _simulate_config_bridge(cfg)
        assert result["TERMINAL_ENV"] == "local"

    def test_terminal_env_type_takes_precedence_over_top_level_backend(self):
        cfg = {"backend": "local", "terminal": {"env_type": "docker"}}
        result = _simulate_config_bridge(cfg)
        assert result["TERMINAL_ENV"] == "docker"

    def test_no_cwd_falls_back_to_messaging_cwd(self):
        cfg = {}
        result = _simulate_config_bridge(cfg, {"MESSAGING_CWD": "/home/hermes/projects"})
        assert result["TERMINAL_CWD"] == "/home/hermes/projects"

    def test_no_cwd_no_messaging_cwd_falls_back_to_hermes_home(self):
        cfg = {}
        result = _simulate_config_bridge(cfg)
        assert result["TERMINAL_CWD"] == "/root/.hermes"

    def test_dot_cwd_triggers_messaging_fallback(self):
        """cwd: '.' should trigger MESSAGING_CWD fallback."""
        cfg = {"cwd": "."}
        result = _simulate_config_bridge(cfg, {"MESSAGING_CWD": "/home/hermes"})
        # "." is stripped but truthy, so it gets set as TERMINAL_CWD
        # Then the MESSAGING_CWD fallback does NOT trigger since TERMINAL_CWD
        # is set and not in (".", "auto", "cwd").
        # Wait — "." IS in the fallback list! So this should fall through.
        # Actually the alias sets it to ".", then the messaging fallback
        # checks if it's in (".", "auto", "cwd") and overrides.
        assert result["TERMINAL_CWD"] == "/home/hermes"

    def test_auto_cwd_triggers_messaging_fallback(self):
        cfg = {"cwd": "auto"}
        result = _simulate_config_bridge(cfg, {"MESSAGING_CWD": "/home/hermes"})
        assert result["TERMINAL_CWD"] == "/home/hermes"

    def test_empty_cwd_ignored(self):
        cfg = {"cwd": ""}
        result = _simulate_config_bridge(cfg, {"MESSAGING_CWD": "/home/hermes"})
        assert result["TERMINAL_CWD"] == "/home/hermes"

    def test_whitespace_only_cwd_ignored(self):
        cfg = {"cwd": "   "}
        result = _simulate_config_bridge(cfg, {"MESSAGING_CWD": "/fallback"})
        assert result["TERMINAL_CWD"] == "/fallback"

    def test_messaging_cwd_env_var_works(self):
        """MESSAGING_CWD in initial env should be picked up as fallback."""
        cfg = {}
        result = _simulate_config_bridge(cfg, {"MESSAGING_CWD": "/home/hermes/projects"})
        assert result["TERMINAL_CWD"] == "/home/hermes/projects"

    def test_top_level_cwd_beats_messaging_cwd(self):
        """Explicit top-level cwd should take precedence over MESSAGING_CWD."""
        cfg = {"cwd": "/from/config"}
        result = _simulate_config_bridge(cfg, {"MESSAGING_CWD": "/from/env"})
        assert result["TERMINAL_CWD"] == "/from/config"


class TestNestedTerminalCwdPlaceholderSkip:
    """terminal.cwd placeholder values must not clobber TERMINAL_CWD.

    When config.yaml has terminal.cwd: "." (or "auto"/"cwd"), the gateway
    config bridge should NOT write that placeholder to TERMINAL_CWD.
    This prevents .env or MESSAGING_CWD values from being overwritten.
    See issues #10225, #4672, #10817.
    """

    def test_terminal_dot_cwd_does_not_clobber_env(self):
        """terminal.cwd: '.' should not overwrite a pre-set TERMINAL_CWD."""
        cfg = {"terminal": {"cwd": "."}}
        result = _simulate_config_bridge(cfg, {"TERMINAL_CWD": "/my/project"})
        assert result["TERMINAL_CWD"] == "/my/project"

    def test_terminal_auto_cwd_does_not_clobber_env(self):
        cfg = {"terminal": {"cwd": "auto"}}
        result = _simulate_config_bridge(cfg, {"TERMINAL_CWD": "/my/project"})
        assert result["TERMINAL_CWD"] == "/my/project"

    def test_terminal_cwd_keyword_does_not_clobber_env(self):
        cfg = {"terminal": {"cwd": "cwd"}}
        result = _simulate_config_bridge(cfg, {"TERMINAL_CWD": "/my/project"})
        assert result["TERMINAL_CWD"] == "/my/project"

    def test_terminal_explicit_cwd_does_override(self):
        """terminal.cwd: '/explicit/path' SHOULD override TERMINAL_CWD."""
        cfg = {"terminal": {"cwd": "/explicit/path"}}
        result = _simulate_config_bridge(cfg, {"TERMINAL_CWD": "/old/value"})
        assert result["TERMINAL_CWD"] == "/explicit/path"

    def test_terminal_dot_cwd_falls_back_to_messaging_cwd(self):
        """terminal.cwd: '.' with no TERMINAL_CWD should fall to MESSAGING_CWD."""
        cfg = {"terminal": {"cwd": "."}}
        result = _simulate_config_bridge(cfg, {"MESSAGING_CWD": "/from/env"})
        assert result["TERMINAL_CWD"] == "/from/env"

    def test_terminal_dot_cwd_and_messaging_cwd_both_set(self):
        """Pre-set TERMINAL_CWD from .env wins over terminal.cwd: '.'."""
        cfg = {"terminal": {"cwd": ".", "backend": "local"}}
        result = _simulate_config_bridge(cfg, {
            "TERMINAL_CWD": "/my/project",
            "MESSAGING_CWD": "/fallback",
        })
        assert result["TERMINAL_CWD"] == "/my/project"

    def test_non_cwd_terminal_keys_still_bridge(self):
        """Other terminal config keys (backend, timeout) should still bridge normally."""
        cfg = {"terminal": {"cwd": ".", "backend": "docker", "timeout": "300"}}
        result = _simulate_config_bridge(cfg, {"MESSAGING_CWD": "/from/env"})
        assert result["TERMINAL_ENV"] == "docker"
        assert result["TERMINAL_TIMEOUT"] == "300"
        assert result.get("TERMINAL_CWD") is None

    def test_docker_placeholder_does_not_inherit_host_home(self):
        """terminal.cwd: '.' + docker + mount off must not resolve to host home."""
        cfg = {
            "terminal": {
                "cwd": ".",
                "backend": "docker",
                "docker_mount_cwd_to_workspace": False,
            },
        }
        result = _simulate_config_bridge(cfg, {"MESSAGING_CWD": "/home/user"})
        assert "TERMINAL_CWD" not in result

    def test_docker_placeholder_mount_on_preserves_messaging_cwd(self):
        """Mount-enabled docker still needs the host cwd signal for /workspace."""
        cfg = {
            "terminal": {
                "cwd": ".",
                "backend": "docker",
                "docker_mount_cwd_to_workspace": True,
            },
        }
        result = _simulate_config_bridge(
            cfg, {"MESSAGING_CWD": "/host/project"}
        )
        assert result["TERMINAL_CWD"] == "/host/project"
        assert result["TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE"] == "True"

    def test_ssh_placeholder_does_not_inherit_host_home(self):
        cfg = {"terminal": {"cwd": "auto", "backend": "ssh"}}
        result = _simulate_config_bridge(cfg, {"MESSAGING_CWD": "/home/user"})
        assert "TERMINAL_CWD" not in result

    def test_local_placeholder_still_falls_back_to_messaging_cwd(self):
        cfg = {"terminal": {"cwd": ".", "backend": "local"}}
        result = _simulate_config_bridge(cfg, {"MESSAGING_CWD": "/home/user"})
        assert result["TERMINAL_CWD"] == "/home/user"

    def test_terminal_home_mode_bridges_to_env(self):
        cfg = {"terminal": {"home_mode": "profile"}}
        result = _simulate_config_bridge(cfg)
        assert result["TERMINAL_HOME_MODE"] == "profile"


class TestTildeExpansion:
    """terminal.cwd values containing shell tilde must be expanded.

    subprocess.Popen does not expand shell syntax, so a literal "~/"
    causes FileNotFoundError.  Regression test for commit 3c42064e.
    """

    def test_terminal_cwd_tilde_expanded(self):
        """terminal.cwd: '~/projects' should expand to /home/<user>/projects."""
        cfg = {"terminal": {"cwd": "~/projects"}}
        result = _simulate_config_bridge(cfg)
        assert result["TERMINAL_CWD"] == os.path.expanduser("~/projects")

    def test_top_level_cwd_tilde_expanded(self):
        """top-level cwd: '~/' should expand to user's home directory."""
        cfg = {"cwd": "~/"}
        result = _simulate_config_bridge(cfg)
        assert result["TERMINAL_CWD"] == os.path.expanduser("~/")

    def test_tilde_with_nested_precedence(self):
        """Nested terminal.cwd should win over top-level, both expanded."""
        cfg = {
            "cwd": "~/top",
            "terminal": {"cwd": "~/nested"},
        }
        result = _simulate_config_bridge(cfg)
        assert result["TERMINAL_CWD"] == os.path.expanduser("~/nested")

    def test_ssh_terminal_cwd_tilde_preserved_for_remote_shell(self, monkeypatch):
        """SSH cwd '~' must mean the remote user's home, not the gateway host HOME."""
        monkeypatch.setenv("HOME", "/opt/data")
        cfg = {"terminal": {"backend": "ssh", "cwd": "~"}}
        result = _simulate_config_bridge(cfg)
        assert result["TERMINAL_ENV"] == "ssh"
        assert result["TERMINAL_CWD"] == "~"

    def test_ssh_terminal_cwd_tilde_child_preserved_for_remote_shell(self, monkeypatch):
        """SSH cwd '~/x' must survive until the SSH shell expands remote HOME."""
        monkeypatch.setenv("HOME", "/opt/data")
        cfg = {"terminal": {"backend": "ssh", "cwd": "~/work"}}
        result = _simulate_config_bridge(cfg)
        assert result["TERMINAL_CWD"] == "~/work"

    def test_ssh_terminal_placeholder_cwd_does_not_fallback_to_host_home(self, monkeypatch):
        """SSH placeholder cwd should let terminal_tool use its remote-home default."""
        monkeypatch.setenv("HOME", "/opt/data")
        cfg = {"terminal": {"backend": "ssh", "cwd": "auto"}}
        result = _simulate_config_bridge(cfg)
        assert "TERMINAL_CWD" not in result


class TestVercelTerminalBridge:
    def test_vercel_terminal_settings_bridge(self):
        cfg = {
            "terminal": {
                "backend": "vercel_sandbox",
                "vercel_runtime": "python3.13",
                "container_persistent": True,
                "container_cpu": 2,
                "container_memory": 4096,
                "container_disk": 51200,
            }
        }
        result = _simulate_config_bridge(cfg, {"MESSAGING_CWD": "/from/env"})
        assert result["TERMINAL_ENV"] == "vercel_sandbox"
        assert result["TERMINAL_VERCEL_RUNTIME"] == "python3.13"
        assert result["TERMINAL_CONTAINER_PERSISTENT"] == "True"
        assert result["TERMINAL_CONTAINER_CPU"] == "2"
        assert result["TERMINAL_CONTAINER_MEMORY"] == "4096"
        assert result["TERMINAL_CONTAINER_DISK"] == "51200"


class TestGatewayProcessCwd:
    def test_local_gateway_chdirs_to_terminal_cwd_for_prompt_builder(
        self, tmp_path, monkeypatch
    ):
        from gateway import run as gateway_run
        import agent.prompt_builder as prompt_builder

        start = tmp_path / "start"
        target = tmp_path / "target"
        start.mkdir()
        target.mkdir()
        monkeypatch.chdir(start)
        monkeypatch.delenv("TERMINAL_ENV", raising=False)
        monkeypatch.setenv("TERMINAL_CWD", str(target))
        monkeypatch.setattr(prompt_builder, "is_wsl", lambda: False)
        prompt_builder._clear_backend_probe_cache()

        assert gateway_run._apply_gateway_process_cwd() is True

        assert Path.cwd() == target
        assert os.environ["TERMINAL_CWD"] == str(target)
        hints = prompt_builder.build_environment_hints()
        assert f"Current working directory: {target}" in hints

    def test_gateway_process_cwd_ignores_remote_backends(self, tmp_path, monkeypatch):
        from gateway import run as gateway_run

        start = tmp_path / "start"
        target = tmp_path / "target"
        start.mkdir()
        target.mkdir()
        monkeypatch.chdir(start)
        monkeypatch.setenv("TERMINAL_ENV", "docker")
        monkeypatch.setenv("TERMINAL_CWD", str(target))

        assert gateway_run._apply_gateway_process_cwd() is False

        assert Path.cwd() == start
        assert os.environ["TERMINAL_CWD"] == str(target)
