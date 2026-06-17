"""Tests for per-profile subprocess HOME isolation (#4426).

Verifies that subprocesses (terminal, execute_code, background processes)
receive a per-profile HOME directory while the Python process's own HOME
and Path.home() remain unchanged.

See: https://github.com/NousResearch/hermes-agent/issues/4426
"""

import os
import threading
from pathlib import Path



# ---------------------------------------------------------------------------
# get_subprocess_home()
# ---------------------------------------------------------------------------

class TestGetSubprocessHome:
    """Unit tests for hermes_constants.get_subprocess_home()."""

    def test_returns_none_when_hermes_home_unset(self, monkeypatch):
        monkeypatch.delenv("HERMES_HOME", raising=False)
        from hermes_constants import get_subprocess_home
        assert get_subprocess_home() is None

    def test_returns_none_when_home_dir_missing(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        # No home/ subdirectory created
        from hermes_constants import get_subprocess_home
        assert get_subprocess_home() is None

    def test_returns_path_when_home_dir_exists(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        profile_home = hermes_home / "home"
        profile_home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        from hermes_constants import get_subprocess_home
        assert get_subprocess_home() == str(profile_home)

    def test_returns_profile_specific_path(self, tmp_path, monkeypatch):
        """Named profiles get their own isolated HOME."""
        profile_dir = tmp_path / ".hermes" / "profiles" / "coder"
        profile_dir.mkdir(parents=True)
        profile_home = profile_dir / "home"
        profile_home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(profile_dir))
        from hermes_constants import get_subprocess_home
        assert get_subprocess_home() == str(profile_home)

    def test_two_profiles_get_different_homes(self, tmp_path, monkeypatch):
        base = tmp_path / ".hermes" / "profiles"
        for name in ("alpha", "beta"):
            p = base / name
            p.mkdir(parents=True)
            (p / "home").mkdir()

        from hermes_constants import get_subprocess_home

        monkeypatch.setenv("HERMES_HOME", str(base / "alpha"))
        home_a = get_subprocess_home()

        monkeypatch.setenv("HERMES_HOME", str(base / "beta"))
        home_b = get_subprocess_home()

        assert home_a is not None
        assert home_b is not None
        assert home_a != home_b
        assert home_a.endswith("alpha/home")
        assert home_b.endswith("beta/home")

    def test_context_override_is_thread_local(self, tmp_path, monkeypatch):
        root = tmp_path / "root"
        profile = tmp_path / "profile"
        root.mkdir()
        profile.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(root))

        from hermes_constants import (
            get_hermes_home,
            reset_hermes_home_override,
            set_hermes_home_override,
        )

        ready = threading.Event()
        release = threading.Event()
        seen: list[str] = []

        def read_from_other_thread():
            ready.set()
            release.wait(timeout=5)
            seen.append(str(get_hermes_home()))

        thread = threading.Thread(target=read_from_other_thread)
        thread.start()
        assert ready.wait(timeout=5)

        token = set_hermes_home_override(profile)
        try:
            assert get_hermes_home() == profile
            release.set()
            thread.join(timeout=5)
        finally:
            reset_hermes_home_override(token)
            release.set()

        assert seen == [str(root)]
        assert get_hermes_home() == root


class TestGetGitHubCliConfigDir:
    """Unit tests for gh config discovery under subprocess HOME isolation."""

    def test_returns_explicit_gh_config_dir(self, monkeypatch):
        monkeypatch.setenv("GH_CONFIG_DIR", "/custom/gh")

        from hermes_constants import get_github_cli_config_dir

        assert get_github_cli_config_dir() == "/custom/gh"

    def test_falls_back_to_real_home_gh_config(self, tmp_path, monkeypatch):
        real_home = tmp_path / "real-home"
        gh_dir = real_home / ".config" / "gh"
        gh_dir.mkdir(parents=True)
        (gh_dir / "hosts.yml").write_text("github.com:\n", encoding="utf-8")
        monkeypatch.setenv("HOME", str(real_home))
        monkeypatch.delenv("GH_CONFIG_DIR", raising=False)

        from hermes_constants import get_github_cli_config_dir

        assert get_github_cli_config_dir() == str(gh_dir)

    def test_prefers_child_home_gh_config_when_present(self, tmp_path, monkeypatch):
        real_home = tmp_path / "real-home"
        child_home = tmp_path / "child-home"
        real_gh = real_home / ".config" / "gh"
        child_gh = child_home / ".config" / "gh"
        real_gh.mkdir(parents=True)
        child_gh.mkdir(parents=True)
        (real_gh / "hosts.yml").write_text("github.com:\n", encoding="utf-8")
        (child_gh / "hosts.yml").write_text("github.com:\n", encoding="utf-8")
        monkeypatch.setenv("HOME", str(real_home))
        monkeypatch.delenv("GH_CONFIG_DIR", raising=False)

        from hermes_constants import get_github_cli_config_dir

        assert get_github_cli_config_dir({"HOME": str(child_home)}) == str(child_gh)

    def test_falls_back_to_os_account_home_when_child_home_is_isolated(self, tmp_path, monkeypatch):
        real_home = tmp_path / "real-home"
        child_home = tmp_path / "isolated-home"
        real_gh = real_home / ".config" / "gh"
        child_home.mkdir()
        real_gh.mkdir(parents=True)
        (real_gh / "hosts.yml").write_text("github.com:\n", encoding="utf-8")
        monkeypatch.setenv("HOME", str(child_home))
        monkeypatch.delenv("GH_CONFIG_DIR", raising=False)

        import hermes_constants
        from hermes_constants import get_github_cli_config_dir

        monkeypatch.setattr(hermes_constants, "_process_user_home", lambda: real_home)

        assert get_github_cli_config_dir({"HOME": str(child_home)}) == str(real_gh)


# ---------------------------------------------------------------------------
# _make_run_env() injection
# ---------------------------------------------------------------------------

class TestMakeRunEnvHomeInjection:
    """Verify _make_run_env() injects HOME into subprocess envs."""

    def test_injects_home_when_profile_home_exists(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        (hermes_home / "home").mkdir()
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.setenv("HOME", "/root")
        monkeypatch.setenv("PATH", "/usr/bin:/bin")

        from tools.environments.local import _make_run_env
        result = _make_run_env({})

        assert result["HOME"] == str(hermes_home / "home")
        assert result["HERMES_HOME"] == str(hermes_home)

    def test_no_injection_when_home_dir_missing(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        # No home/ subdirectory
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.setenv("HOME", "/root")
        monkeypatch.setenv("PATH", "/usr/bin:/bin")

        from tools.environments.local import _make_run_env
        result = _make_run_env({})

        assert result["HOME"] == "/root"

    def test_no_injection_when_hermes_home_unset(self, monkeypatch):
        monkeypatch.delenv("HERMES_HOME", raising=False)
        monkeypatch.setenv("HOME", "/home/user")
        monkeypatch.setenv("PATH", "/usr/bin:/bin")

        from tools.environments.local import _make_run_env
        result = _make_run_env({})

        assert result["HOME"] == "/home/user"

    def test_context_override_bridges_to_subprocess_env(self, tmp_path, monkeypatch):
        root = tmp_path / "root"
        profile = tmp_path / "profile"
        root.mkdir()
        profile.mkdir()
        (profile / "home").mkdir()
        monkeypatch.setenv("HERMES_HOME", str(root))
        monkeypatch.setenv("HOME", "/root")
        monkeypatch.setenv("PATH", "/usr/bin:/bin")

        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
        from tools.environments.local import _make_run_env

        token = set_hermes_home_override(profile)
        try:
            result = _make_run_env({})
        finally:
            reset_hermes_home_override(token)

        assert result["HERMES_HOME"] == str(profile)
        assert result["HOME"] == str(profile / "home")

    def test_injects_gh_config_dir_when_home_is_isolated(self, tmp_path, monkeypatch):
        real_home = tmp_path / "real-home"
        hermes_home = tmp_path / "hermes"
        gh_dir = real_home / ".config" / "gh"
        gh_dir.mkdir(parents=True)
        (gh_dir / "hosts.yml").write_text("github.com:\n", encoding="utf-8")
        hermes_home.mkdir()
        (hermes_home / "home").mkdir()
        monkeypatch.setenv("HOME", str(real_home))
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.delenv("GH_CONFIG_DIR", raising=False)

        from tools.environments.local import _make_run_env
        result = _make_run_env({})

        assert result["HOME"] == str(hermes_home / "home")
        assert result["GH_CONFIG_DIR"] == str(gh_dir)
        assert "GH_TOKEN" not in result
        assert "GITHUB_TOKEN" not in result

    def test_bridges_real_home_cli_paths_when_home_is_isolated(self, tmp_path, monkeypatch):
        real_home = tmp_path / "real-home"
        hermes_home = tmp_path / "hermes"
        real_bin = real_home / ".local" / "bin"
        profile_home = hermes_home / "home"
        profile_local_bin = profile_home / ".local" / "bin"
        profile_foundry_bin = profile_home / ".foundry" / "bin"
        profile_cargo_bin = profile_home / ".cargo" / "bin"
        gh_dir = real_home / ".config" / "gh"
        gcloud_dir = real_home / ".config" / "gcloud"
        docker_dir = real_home / ".docker"
        codex_home = real_home / ".codex"
        gitconfig = real_home / ".gitconfig"
        npmrc = real_home / ".npmrc"
        real_bin.mkdir(parents=True)
        profile_local_bin.mkdir(parents=True)
        profile_foundry_bin.mkdir(parents=True)
        profile_cargo_bin.mkdir(parents=True)
        gh_dir.mkdir(parents=True)
        gcloud_dir.mkdir(parents=True)
        docker_dir.mkdir(parents=True)
        codex_home.mkdir(parents=True)
        gitconfig.write_text("[user]\n\temail = user@example.com\n", encoding="utf-8")
        (docker_dir / "config.json").write_text("{}\n", encoding="utf-8")
        npmrc.write_text("registry=https://registry.npmjs.org/\n", encoding="utf-8")
        (gh_dir / "hosts.yml").write_text("github.com:\n", encoding="utf-8")
        profile_home.mkdir(exist_ok=True)
        monkeypatch.setattr(Path, "home", lambda: real_home)
        monkeypatch.setenv("HOME", str(real_home))
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.delenv("GH_CONFIG_DIR", raising=False)
        monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
        monkeypatch.delenv("DOCKER_CONFIG", raising=False)
        monkeypatch.delenv("CODEX_HOME", raising=False)
        monkeypatch.delenv("CLOUDSDK_CONFIG", raising=False)
        monkeypatch.delenv("NPM_CONFIG_USERCONFIG", raising=False)

        from tools.environments.local import _make_run_env
        result = _make_run_env({})

        assert result["HOME"] == str(profile_home)
        assert result["HERMES_HOME"] == str(hermes_home)
        path_entries = result["PATH"].split(os.pathsep)
        assert path_entries[:3] == [
            str(profile_local_bin),
            str(profile_foundry_bin),
            str(profile_cargo_bin),
        ]
        assert str(real_bin) in path_entries
        assert result["GH_CONFIG_DIR"] == str(gh_dir)
        assert result["GIT_CONFIG_GLOBAL"] == str(gitconfig)
        assert result["DOCKER_CONFIG"] == str(docker_dir)
        assert result["CODEX_HOME"] == str(codex_home)
        assert result["CLOUDSDK_CONFIG"] == str(gcloud_dir)
        assert result["NPM_CONFIG_USERCONFIG"] == str(npmrc)

    def test_prefers_real_home_gh_config_over_profile_home_config(
        self, tmp_path, monkeypatch
    ):
        real_home = tmp_path / "real-home"
        hermes_home = tmp_path / "hermes"
        real_gh = real_home / ".config" / "gh"
        fake_gh = hermes_home / "home" / ".config" / "gh"
        real_gh.mkdir(parents=True)
        fake_gh.mkdir(parents=True)
        (real_gh / "hosts.yml").write_text("github.com:\n", encoding="utf-8")
        (fake_gh / "hosts.yml").write_text("github.com:\n", encoding="utf-8")
        hermes_home.mkdir(exist_ok=True)
        monkeypatch.setattr(Path, "home", lambda: real_home)
        monkeypatch.setenv("HOME", str(real_home))
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.delenv("GH_CONFIG_DIR", raising=False)

        from tools.environments.local import _make_run_env
        result = _make_run_env({})

        assert result["HOME"] == str(hermes_home / "home")
        assert result["GH_CONFIG_DIR"] == str(real_gh)

    def test_explicit_profile_bridge_values_are_not_overridden(self, tmp_path, monkeypatch):
        real_home = tmp_path / "real-home"
        hermes_home = tmp_path / "hermes"
        real_bin = real_home / ".local" / "bin"
        gh_dir = real_home / ".config" / "gh"
        gcloud_dir = real_home / ".config" / "gcloud"
        docker_dir = real_home / ".docker"
        codex_home = real_home / ".codex"
        gitconfig = real_home / ".gitconfig"
        npmrc = real_home / ".npmrc"
        real_bin.mkdir(parents=True)
        gh_dir.mkdir(parents=True)
        gcloud_dir.mkdir(parents=True)
        docker_dir.mkdir(parents=True)
        codex_home.mkdir(parents=True)
        gitconfig.write_text("[user]\n\temail = user@example.com\n", encoding="utf-8")
        (docker_dir / "config.json").write_text("{}\n", encoding="utf-8")
        npmrc.write_text("registry=https://registry.npmjs.org/\n", encoding="utf-8")
        (gh_dir / "hosts.yml").write_text("github.com:\n", encoding="utf-8")
        hermes_home.mkdir()
        (hermes_home / "home").mkdir()
        monkeypatch.setattr(Path, "home", lambda: real_home)
        monkeypatch.setenv("HOME", str(real_home))
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.setenv("PATH", "/usr/bin:/bin")

        from tools.environments.local import _make_run_env
        result = _make_run_env({
            "HERMES_HOME": "/explicit/hermes",
            "PATH": "/explicit/bin",
            "GH_CONFIG_DIR": "/explicit/gh",
            "GIT_CONFIG_GLOBAL": "/explicit/gitconfig",
            "DOCKER_CONFIG": "/explicit/docker",
            "CODEX_HOME": "/explicit/codex",
            "CLOUDSDK_CONFIG": "/explicit/gcloud",
            "NPM_CONFIG_USERCONFIG": "/explicit/npmrc",
        })

        assert result["HOME"] == str(hermes_home / "home")
        assert result["HERMES_HOME"] == "/explicit/hermes"
        assert result["PATH"] == "/explicit/bin"
        assert result["GH_CONFIG_DIR"] == "/explicit/gh"
        assert result["GIT_CONFIG_GLOBAL"] == "/explicit/gitconfig"
        assert result["DOCKER_CONFIG"] == "/explicit/docker"
        assert result["CODEX_HOME"] == "/explicit/codex"
        assert result["CLOUDSDK_CONFIG"] == "/explicit/gcloud"
        assert result["NPM_CONFIG_USERCONFIG"] == "/explicit/npmrc"

    def test_strips_kanban_routing_env_from_terminal_run_env(self, tmp_path, monkeypatch):
        """Terminal commands must not inherit a worker's live board DB scope."""
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        control_values = {
            "HERMES_KANBAN_DB": str(tmp_path / "live" / "kanban.db"),
            "HERMES_KANBAN_BOARD": "discord-1512698251977949295",
            "HERMES_KANBAN_HOME": str(tmp_path / "kanban-home"),
            "HERMES_KANBAN_ROOT": str(tmp_path / "legacy-root"),
            "HERMES_KANBAN_WORKSPACES_ROOT": str(tmp_path / "workspaces"),
            "HERMES_KANBAN_WORKSPACE": str(tmp_path / "workspace"),
            "HERMES_KANBAN_BRANCH": "fix/live-board",
            "HERMES_KANBAN_TASK": "t_live",
            "HERMES_KANBAN_RUN_ID": "42",
            "HERMES_KANBAN_CLAIM_LOCK": "claim-live",
        }
        for key, value in control_values.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("HERMES_KANBAN_BUSY_TIMEOUT_MS", "12345")
        monkeypatch.setenv("_HERMES_FORCE_HERMES_KANBAN_DB", str(tmp_path / "forced" / "kanban.db"))
        monkeypatch.setenv("_HERMES_FORCE_HERMES_KANBAN_BOARD", "forced-board")

        from tools.environments.local import _make_run_env
        result = _make_run_env({
            "HERMES_KANBAN_BOARD": "attempted-extra",
            "_HERMES_FORCE_HERMES_KANBAN_TASK": "forced-task",
        })

        for key in control_values:
            assert key not in result
        assert result["HERMES_KANBAN_BUSY_TIMEOUT_MS"] == "12345"


# ---------------------------------------------------------------------------
# _sanitize_subprocess_env() injection
# ---------------------------------------------------------------------------

class TestSanitizeSubprocessEnvHomeInjection:
    """Verify _sanitize_subprocess_env() injects HOME for background procs."""

    def test_injects_home_when_profile_home_exists(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        (hermes_home / "home").mkdir()
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        base_env = {"HOME": "/root", "PATH": "/usr/bin", "USER": "root"}
        from tools.environments.local import _sanitize_subprocess_env
        result = _sanitize_subprocess_env(base_env)

        assert result["HOME"] == str(hermes_home / "home")
        assert result["HERMES_HOME"] == str(hermes_home)

    def test_no_injection_when_home_dir_missing(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        base_env = {"HOME": "/root", "PATH": "/usr/bin"}
        from tools.environments.local import _sanitize_subprocess_env
        result = _sanitize_subprocess_env(base_env)

        assert result["HOME"] == "/root"

    def test_context_override_bridges_to_background_env(self, tmp_path, monkeypatch):
        root = tmp_path / "root"
        profile = tmp_path / "profile"
        root.mkdir()
        profile.mkdir()
        (profile / "home").mkdir()
        monkeypatch.setenv("HERMES_HOME", str(root))

        base_env = {"HOME": "/root", "PATH": "/usr/bin"}
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
        from tools.environments.local import _sanitize_subprocess_env

        token = set_hermes_home_override(profile)
        try:
            result = _sanitize_subprocess_env(base_env)
        finally:
            reset_hermes_home_override(token)

        assert result["HERMES_HOME"] == str(profile)
        assert result["HOME"] == str(profile / "home")

    def test_injects_gh_config_dir_when_background_home_is_isolated(
        self, tmp_path, monkeypatch
    ):
        real_home = tmp_path / "real-home"
        hermes_home = tmp_path / "hermes"
        gh_dir = real_home / ".config" / "gh"
        gh_dir.mkdir(parents=True)
        (gh_dir / "hosts.yml").write_text("github.com:\n", encoding="utf-8")
        hermes_home.mkdir()
        (hermes_home / "home").mkdir()
        monkeypatch.setenv("HOME", str(real_home))
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.delenv("GH_CONFIG_DIR", raising=False)

        base_env = {
            "HOME": str(real_home),
            "PATH": "/usr/bin",
            "GH_TOKEN": "gho_secret",
            "GITHUB_TOKEN": "ghp_secret",
        }
        from tools.environments.local import _sanitize_subprocess_env
        result = _sanitize_subprocess_env(base_env)

        assert result["HOME"] == str(hermes_home / "home")
        assert result["GH_CONFIG_DIR"] == str(gh_dir)
        assert "GH_TOKEN" not in result
        assert "GITHUB_TOKEN" not in result

    def test_background_bridges_real_home_cli_paths_when_home_is_isolated(
        self, tmp_path, monkeypatch
    ):
        real_home = tmp_path / "real-home"
        hermes_home = tmp_path / "hermes"
        real_bin = real_home / ".local" / "bin"
        profile_home = hermes_home / "home"
        profile_local_bin = profile_home / ".local" / "bin"
        profile_foundry_bin = profile_home / ".foundry" / "bin"
        profile_cargo_bin = profile_home / ".cargo" / "bin"
        gh_dir = real_home / ".config" / "gh"
        gcloud_dir = real_home / ".config" / "gcloud"
        docker_dir = real_home / ".docker"
        codex_home = real_home / ".codex"
        gitconfig = real_home / ".gitconfig"
        npmrc = real_home / ".npmrc"
        real_bin.mkdir(parents=True)
        profile_local_bin.mkdir(parents=True)
        profile_foundry_bin.mkdir(parents=True)
        profile_cargo_bin.mkdir(parents=True)
        gh_dir.mkdir(parents=True)
        gcloud_dir.mkdir(parents=True)
        docker_dir.mkdir(parents=True)
        codex_home.mkdir(parents=True)
        gitconfig.write_text("[user]\n\temail = user@example.com\n", encoding="utf-8")
        (docker_dir / "config.json").write_text("{}\n", encoding="utf-8")
        npmrc.write_text("registry=https://registry.npmjs.org/\n", encoding="utf-8")
        (gh_dir / "hosts.yml").write_text("github.com:\n", encoding="utf-8")
        profile_home.mkdir(exist_ok=True)
        monkeypatch.setattr(Path, "home", lambda: real_home)
        monkeypatch.setenv("HOME", str(real_home))
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.delenv("GH_CONFIG_DIR", raising=False)
        monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
        monkeypatch.delenv("DOCKER_CONFIG", raising=False)
        monkeypatch.delenv("CODEX_HOME", raising=False)
        monkeypatch.delenv("CLOUDSDK_CONFIG", raising=False)
        monkeypatch.delenv("NPM_CONFIG_USERCONFIG", raising=False)

        base_env = {"HOME": str(real_home), "PATH": "/usr/bin:/bin"}
        from tools.environments.local import _sanitize_subprocess_env
        result = _sanitize_subprocess_env(base_env)

        assert result["HOME"] == str(profile_home)
        assert result["HERMES_HOME"] == str(hermes_home)
        path_entries = result["PATH"].split(os.pathsep)
        assert path_entries[:3] == [
            str(profile_local_bin),
            str(profile_foundry_bin),
            str(profile_cargo_bin),
        ]
        assert str(real_bin) in path_entries
        assert result["GH_CONFIG_DIR"] == str(gh_dir)
        assert result["GIT_CONFIG_GLOBAL"] == str(gitconfig)
        assert result["DOCKER_CONFIG"] == str(docker_dir)
        assert result["CODEX_HOME"] == str(codex_home)
        assert result["CLOUDSDK_CONFIG"] == str(gcloud_dir)
        assert result["NPM_CONFIG_USERCONFIG"] == str(npmrc)

    def test_background_explicit_profile_bridge_values_are_not_overridden(
        self, tmp_path, monkeypatch
    ):
        real_home = tmp_path / "real-home"
        hermes_home = tmp_path / "hermes"
        real_bin = real_home / ".local" / "bin"
        gh_dir = real_home / ".config" / "gh"
        gcloud_dir = real_home / ".config" / "gcloud"
        docker_dir = real_home / ".docker"
        codex_home = real_home / ".codex"
        gitconfig = real_home / ".gitconfig"
        npmrc = real_home / ".npmrc"
        real_bin.mkdir(parents=True)
        gh_dir.mkdir(parents=True)
        gcloud_dir.mkdir(parents=True)
        docker_dir.mkdir(parents=True)
        codex_home.mkdir(parents=True)
        gitconfig.write_text("[user]\n\temail = user@example.com\n", encoding="utf-8")
        (docker_dir / "config.json").write_text("{}\n", encoding="utf-8")
        npmrc.write_text("registry=https://registry.npmjs.org/\n", encoding="utf-8")
        (gh_dir / "hosts.yml").write_text("github.com:\n", encoding="utf-8")
        hermes_home.mkdir()
        (hermes_home / "home").mkdir()
        monkeypatch.setattr(Path, "home", lambda: real_home)
        monkeypatch.setenv("HOME", str(real_home))
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        base_env = {"HOME": str(real_home), "PATH": "/usr/bin:/bin"}
        extra_env = {
            "HERMES_HOME": "/explicit/hermes",
            "PATH": "/explicit/bin",
            "GH_CONFIG_DIR": "/explicit/gh",
            "GIT_CONFIG_GLOBAL": "/explicit/gitconfig",
            "DOCKER_CONFIG": "/explicit/docker",
            "CODEX_HOME": "/explicit/codex",
            "CLOUDSDK_CONFIG": "/explicit/gcloud",
            "NPM_CONFIG_USERCONFIG": "/explicit/npmrc",
        }
        from tools.environments.local import _sanitize_subprocess_env
        result = _sanitize_subprocess_env(base_env, extra_env)

        assert result["HOME"] == str(hermes_home / "home")
        assert result["HERMES_HOME"] == "/explicit/hermes"
        assert result["PATH"] == "/explicit/bin"
        assert result["GH_CONFIG_DIR"] == "/explicit/gh"
        assert result["GIT_CONFIG_GLOBAL"] == "/explicit/gitconfig"
        assert result["DOCKER_CONFIG"] == "/explicit/docker"
        assert result["CODEX_HOME"] == "/explicit/codex"
        assert result["CLOUDSDK_CONFIG"] == "/explicit/gcloud"
        assert result["NPM_CONFIG_USERCONFIG"] == "/explicit/npmrc"

    def test_strips_kanban_routing_env_from_background_env(self, tmp_path, monkeypatch):
        control_values = {
            "HERMES_KANBAN_DB": str(tmp_path / "live" / "kanban.db"),
            "HERMES_KANBAN_BOARD": "discord-1512698251977949295",
            "HERMES_KANBAN_HOME": str(tmp_path / "kanban-home"),
            "HERMES_KANBAN_ROOT": str(tmp_path / "legacy-root"),
            "HERMES_KANBAN_WORKSPACES_ROOT": str(tmp_path / "workspaces"),
            "HERMES_KANBAN_WORKSPACE": str(tmp_path / "workspace"),
            "HERMES_KANBAN_BRANCH": "fix/live-board",
            "HERMES_KANBAN_TASK": "t_live",
            "HERMES_KANBAN_RUN_ID": "42",
            "HERMES_KANBAN_CLAIM_LOCK": "claim-live",
        }
        base_env = {
            "PATH": "/usr/bin",
            "HERMES_KANBAN_BUSY_TIMEOUT_MS": "12345",
            "_HERMES_FORCE_HERMES_KANBAN_DB": str(tmp_path / "forced" / "kanban.db"),
            **control_values,
        }
        extra_env = {
            "HERMES_KANBAN_BOARD": "attempted-extra",
            "_HERMES_FORCE_HERMES_KANBAN_TASK": "forced-task",
        }
        monkeypatch.setenv("HERMES_KANBAN_BUSY_TIMEOUT_MS", "12345")

        from tools.environments.local import _sanitize_subprocess_env
        result = _sanitize_subprocess_env(base_env, extra_env)

        for key in control_values:
            assert key not in result
        assert result["HERMES_KANBAN_BUSY_TIMEOUT_MS"] == "12345"


# ---------------------------------------------------------------------------
# Profile bootstrap
# ---------------------------------------------------------------------------

class TestProfileBootstrap:
    """Verify new profiles get a home/ subdirectory."""

    def test_profile_dirs_includes_home(self):
        from hermes_cli.profiles import _PROFILE_DIRS
        assert "home" in _PROFILE_DIRS

    def test_create_profile_bootstraps_home_dir(self, tmp_path, monkeypatch):
        """create_profile() should create home/ inside the profile dir."""
        home = tmp_path / ".hermes"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(home))

        from hermes_cli.profiles import create_profile
        profile_dir = create_profile("testbot", no_alias=True)
        assert (profile_dir / "home").is_dir()


# ---------------------------------------------------------------------------
# Python process HOME unchanged
# ---------------------------------------------------------------------------

class TestPythonProcessUnchanged:
    """Confirm the Python process's own HOME is never modified."""

    def test_path_home_unchanged_after_subprocess_home_resolved(
        self, tmp_path, monkeypatch
    ):
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        (hermes_home / "home").mkdir()
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        original_home = os.environ.get("HOME")
        original_path_home = str(Path.home())

        from hermes_constants import get_subprocess_home
        sub_home = get_subprocess_home()

        # Subprocess home is set but Python HOME stays the same
        assert sub_home is not None
        assert os.environ.get("HOME") == original_home
        assert str(Path.home()) == original_path_home
