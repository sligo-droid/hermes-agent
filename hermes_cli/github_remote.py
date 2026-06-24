"""GitHub CLI and remote preflight helpers for PR automation."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from hermes_constants import get_github_cli_config_dir

_GITHUB_TOKEN_ENV_KEYS = frozenset({"GH_TOKEN", "GITHUB_TOKEN"})
_GITHUB_CLI_ENV_KEYS = frozenset(
    {
        "ALL_PROXY",
        "CURL_CA_BUNDLE",
        "GIT_CONFIG_GLOBAL",
        "GIT_SSH",
        "GIT_SSH_COMMAND",
        "GH_CONFIG_DIR",
        "HOME",
        "HERMES_HOME",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "LANG",
        "LC_ALL",
        "LOGNAME",
        "NO_PROXY",
        "PATH",
        "REQUESTS_CA_BUNDLE",
        "SHELL",
        "SSH_AGENT_PID",
        "SSH_AUTH_SOCK",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TEMP",
        "TERM",
        "TMP",
        "TMPDIR",
        "USER",
        "XDG_CONFIG_HOME",
        "all_proxy",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)


@dataclass(frozen=True)
class GitRemote:
    name: str
    url: str


def github_cli_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    """Return a sanitized env for direct ``gh``/GitHub-backed git subprocesses."""
    source = dict(os.environ if base_env is None else base_env)
    env = {
        key: value
        for key, value in source.items()
        if key in _GITHUB_CLI_ENV_KEYS and key not in _GITHUB_TOKEN_ENV_KEYS
    }
    if not env.get("GIT_CONFIG_GLOBAL"):
        git_config = _os_user_home_gitconfig()
        if git_config:
            env["GIT_CONFIG_GLOBAL"] = str(git_config)

    try:
        gh_config_dir = get_github_cli_config_dir(env)
    except Exception:
        gh_config_dir = None
    if gh_config_dir:
        env["GH_CONFIG_DIR"] = gh_config_dir
    return env


def _os_user_home_gitconfig() -> Path | None:
    candidates: list[Path] = []
    try:
        import pwd

        home = str(pwd.getpwuid(os.getuid()).pw_dir or "").strip()
        if home:
            candidates.append(Path(home) / ".gitconfig")
    except Exception:
        pass
    candidates.append(Path.home() / ".gitconfig")
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if candidate.is_file():
            return candidate
    return None


def github_repo_from_url(url: str) -> str | None:
    raw = str(url or "").strip()
    if not raw:
        return None
    raw = re.sub(r"^git\+", "", raw)
    patterns = (
        r"^https?://github\.com/([^/]+)/([^/#?]+?)(?:\.git)?/?(?:[#?].*)?$",
        r"^ssh://git@github\.com/([^/]+)/([^/#?]+?)(?:\.git)?/?(?:[#?].*)?$",
        r"^git@github\.com:([^/]+)/([^/#?]+?)(?:\.git)?$",
    )
    for pattern in patterns:
        match = re.match(pattern, raw)
        if match:
            owner, repo = match.groups()
            return f"{owner}/{repo}"
    return None


def github_repo_from_value(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if re.match(r"^[^/\s]+/[^/\s]+$", raw):
        return raw.removesuffix(".git")
    return github_repo_from_url(raw)


def _remote_lines(root: Path) -> list[str] | None:
    try:
        result = subprocess.run(
            ["git", "remote", "-v"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return (result.stdout or "").splitlines()


def _parse_remote_lines(lines: Iterable[str]) -> list[GitRemote]:
    remotes: list[GitRemote] = []
    seen: set[tuple[str, str]] = set()
    for line in lines:
        parts = line.split()
        if len(parts) < 2:
            continue
        remote = GitRemote(name=parts[0].strip(), url=parts[1].strip())
        key = (remote.name, remote.url)
        if remote.name and remote.url and key not in seen:
            seen.add(key)
            remotes.append(remote)
    return remotes


def _is_local_or_file_remote(url: str) -> bool:
    raw = str(url or "").strip()
    if not raw or github_repo_from_url(raw):
        return False
    if raw.startswith("file://"):
        return True
    if raw.startswith(("/", "./", "../", "~")):
        return True
    if re.match(r"^[A-Za-z]:[\\/]", raw):
        return True
    return False


def github_remote_preflight_error(
    root: str | Path,
    *,
    operation: str = "create/finalize PR",
) -> str | None:
    """Return a clear error when a checkout cannot support GitHub PR commands.

    The worker finalizer pushes to the checkout remote before running ``gh pr``
    commands. A local-only remote such as ``/home/droid/hermes`` is a repo
    boundary problem, not a GitHub authentication problem.
    """
    lines = _remote_lines(Path(root))
    if lines is None:
        return None
    remotes = _parse_remote_lines(lines)
    if not remotes:
        return None
    has_github_remote = any(github_repo_from_url(remote.url) for remote in remotes)
    origin_remotes = [remote for remote in remotes if remote.name == "origin"]
    origin_has_github = any(github_repo_from_url(remote.url) for remote in origin_remotes)
    if origin_remotes and has_github_remote and not origin_has_github:
        local_origin = all(_is_local_or_file_remote(remote.url) for remote in origin_remotes)
        descriptor = "origin remote is local/file" if local_origin else "origin remote is not GitHub"
        return _format_remote_preflight_error(
            operation=operation,
            descriptor=descriptor,
            remotes=origin_remotes,
        )
    if has_github_remote:
        return None

    local_only = all(_is_local_or_file_remote(remote.url) for remote in remotes)
    descriptor = "only local/file remotes" if local_only else "no GitHub remote"
    return _format_remote_preflight_error(
        operation=operation,
        descriptor=descriptor,
        remotes=remotes,
    )


def github_origin_repo(root: str | Path) -> str | None:
    """Return the normalized GitHub owner/repo for ``git push origin``.

    ``git remote -v`` prints both fetch and push URLs. If they differ, ``git
    push origin ...`` uses the push URL, so prefer the explicit ``(push)`` line
    when present and fall back to the first origin GitHub URL otherwise.
    """
    lines = _remote_lines(Path(root))
    if lines is None:
        return None
    fallback: str | None = None
    for line in lines:
        parts = line.split()
        if len(parts) < 2 or parts[0].strip() != "origin":
            continue
        repo = github_repo_from_url(parts[1].strip())
        if not repo:
            continue
        if fallback is None:
            fallback = repo
        if len(parts) >= 3 and parts[2].strip() == "(push)":
            return repo
    return fallback


def _format_remote_preflight_error(
    *,
    operation: str,
    descriptor: str,
    remotes: list[GitRemote],
) -> str:
    rendered = ", ".join(f"{remote.name}={remote.url}" for remote in remotes[:4])
    if len(remotes) > 4:
        rendered = f"{rendered}, ..."
    return (
        f"Cannot {operation}: checkout has {descriptor}"
        f"{f' ({rendered})' if rendered else ''}. "
        "This is not a GitHub token/auth problem. Set a GitHub remote before "
        "PR finalization, for example: "
        "`git remote set-url origin git@github.com:OWNER/REPO.git`."
    )
