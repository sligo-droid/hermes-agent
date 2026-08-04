"""Pinned GBrain subprocess client with an allowlisted environment."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from hermes_cli.config import cfg_get, load_config

from .scope import ClientKnowledgeValidationError


PINNED_GBRAIN_TAG = "v0.42.73.1"
PINNED_GBRAIN_COMMIT = "aecb33e795cc4806f760446c55ab1c350194ddc8"
PINNED_GBRAIN_VERSION = "0.42.73.1"
PINNED_BUN_VERSION = "1.3.14"
DEFAULT_SOURCE_ID = "client-knowledge"
MAX_RAW_RESULT_CHARS = 1_000_000

_ENV_ALLOWLIST = ("LANG", "LC_ALL", "TZ")


@dataclass(frozen=True)
class GBrainSettings:
    executable: Path
    home: Path
    checkout: Path | None = None
    args: tuple[str, ...] = ()
    source_id: str = DEFAULT_SOURCE_ID
    timeout_seconds: float = 30.0
    max_context_chars: int = 8_000


def load_settings(config: Mapping[str, Any] | None = None) -> GBrainSettings:
    cfg = dict(config or load_config() or {})
    block = cfg_get(cfg, "client_knowledge", "gbrain", default={})
    if not isinstance(block, dict):
        block = {}
    executable = Path(str(block.get("executable") or "")).expanduser()
    home = Path(str(block.get("home") or "")).expanduser()
    checkout = Path(str(block.get("checkout") or "")).expanduser()
    if not executable.is_absolute() or not executable.is_file() or not os.access(executable, os.X_OK):
        raise ClientKnowledgeValidationError(
            "client_knowledge.gbrain.executable must be an absolute executable path"
        )
    if not home.is_absolute() or not home.is_dir():
        raise ClientKnowledgeValidationError(
            "client_knowledge.gbrain.home must be an absolute isolated GBrain home"
        )
    if not checkout.is_absolute() or not checkout.is_dir():
        raise ClientKnowledgeValidationError(
            "client_knowledge.gbrain.checkout must be an absolute pinned GBrain checkout"
        )
    checkout = checkout.resolve()
    source_id = str(block.get("source_id") or DEFAULT_SOURCE_ID).strip()
    if source_id != DEFAULT_SOURCE_ID:
        raise ClientKnowledgeValidationError("client knowledge source_id must be client-knowledge")
    timeout = float(block.get("timeout_seconds") or 30)
    max_context = int(block.get("max_context_chars") or 8_000)
    raw_args = block.get("args") or []
    if not isinstance(raw_args, list) or not all(isinstance(item, str) and item for item in raw_args):
        raise ClientKnowledgeValidationError("client_knowledge.gbrain.args must be a list of strings")
    for item in raw_args:
        if item.startswith("/") and not Path(item).is_file():
            raise ClientKnowledgeValidationError("absolute GBrain launcher arguments must name files")
    source_launcher = checkout / "src" / "cli.ts"
    if source_launcher.is_symlink() or not source_launcher.is_file():
        raise ClientKnowledgeValidationError(
            "pinned GBrain checkout src/cli.ts must be a regular file"
        )
    resolved_args = tuple(str(Path(item).resolve()) if item.startswith("/") else item for item in raw_args)
    if resolved_args != (str(source_launcher.resolve()),):
        raise ClientKnowledgeValidationError(
            "GBrain args must contain only the pinned checkout src/cli.ts launcher"
        )
    return GBrainSettings(
        executable=executable.resolve(),
        home=home.resolve(),
        checkout=checkout,
        args=resolved_args,
        source_id=source_id,
        timeout_seconds=max(1.0, min(timeout, 120.0)),
        max_context_chars=max(1_000, min(max_context, 32_000)),
    )


def client_knowledge_environment(settings: GBrainSettings) -> dict[str, str]:
    env = {key: os.environ[key] for key in _ENV_ALLOWLIST if os.environ.get(key)}
    env.update(
        {
            # Pinned GBrain treats GBRAIN_HOME as a parent root and creates
            # ``.gbrain`` below it. Keep generic HOME on that same isolated
            # root so no secondary config/cache path can escape the boundary.
            "HOME": str(settings.home),
            "GBRAIN_HOME": str(settings.home),
            "PATH": str(settings.executable.parent),
            "LANG": env.get("LANG", "C.UTF-8"),
            "LC_ALL": env.get("LC_ALL", "C.UTF-8"),
            "TZ": env.get("TZ", "UTC"),
            "NODE_ENV": "test",
            "GBRAIN_SKIP_STARTUP_HOOKS": "1",
            "GBRAIN_NO_BANNER": "1",
        }
    )
    return env


class GBrainClient:
    def __init__(self, settings: GBrainSettings):
        self.settings = settings

    def _run(self, args: list[str]) -> Any:
        command = [str(self.settings.executable), *self.settings.args, *args]
        completed = subprocess.run(
            command,
            cwd=str(self.settings.home),
            env=client_knowledge_environment(self.settings),
            capture_output=True,
            text=True,
            timeout=self.settings.timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            stderr = completed.stderr.strip()
            raise RuntimeError(f"pinned GBrain command failed ({completed.returncode}): {stderr[:500]}")
        stdout = completed.stdout
        if len(stdout) > MAX_RAW_RESULT_CHARS:
            raise RuntimeError("pinned GBrain response exceeded the raw result limit")
        try:
            return json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("pinned GBrain returned malformed JSON") from exc

    def assert_pinned_version(self) -> None:
        bun = subprocess.run(
            [str(self.settings.executable), "--version"],
            cwd=str(self.settings.home),
            env=client_knowledge_environment(self.settings),
            capture_output=True,
            text=True,
            timeout=self.settings.timeout_seconds,
            check=False,
        )
        if bun.returncode != 0 or bun.stdout.strip() != PINNED_BUN_VERSION:
            raise RuntimeError(f"client knowledge requires Bun {PINNED_BUN_VERSION}")
        completed = subprocess.run(
            [str(self.settings.executable), *self.settings.args, "--version"],
            cwd=str(self.settings.home),
            env=client_knowledge_environment(self.settings),
            capture_output=True,
            text=True,
            timeout=self.settings.timeout_seconds,
            check=False,
        )
        if completed.returncode != 0 or completed.stdout.strip() != f"gbrain {PINNED_GBRAIN_VERSION}":
            raise RuntimeError(
                f"client knowledge requires GBrain {PINNED_GBRAIN_TAG} ({PINNED_GBRAIN_COMMIT})"
            )

    def assert_pinned_checkout(self) -> None:
        checkout = self.settings.checkout
        if checkout is None:
            raise RuntimeError("client knowledge requires an approved pinned GBrain checkout")
        package_path = checkout / "package.json"
        try:
            package = json.loads(package_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("pinned GBrain checkout has no valid package.json") from exc
        if package.get("version") != PINNED_GBRAIN_VERSION:
            raise RuntimeError("pinned GBrain checkout package version does not match")
        git = shutil.which("git")
        if not git:
            raise RuntimeError("client knowledge requires Git for checkout verification")
        git_env = {
            "PATH": str(Path(git).parent),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "TZ": "UTC",
            "HOME": str(self.settings.home),
        }
        completed = subprocess.run(
            [git, "-C", str(checkout), "rev-parse", "HEAD"],
            env=git_env,
            capture_output=True,
            text=True,
            timeout=self.settings.timeout_seconds,
            check=False,
        )
        if completed.returncode != 0 or completed.stdout.strip() != PINNED_GBRAIN_COMMIT:
            raise RuntimeError(
                f"client knowledge requires GBrain {PINNED_GBRAIN_TAG} ({PINNED_GBRAIN_COMMIT})"
            )
        status = subprocess.run(
            [git, "-C", str(checkout), "status", "--porcelain", "--untracked-files=normal"],
            env=git_env,
            capture_output=True,
            text=True,
            timeout=self.settings.timeout_seconds,
            check=False,
        )
        if status.returncode != 0 or status.stdout.strip():
            raise RuntimeError("pinned GBrain checkout has modified or untracked files")

    def assert_keyword_only(self) -> None:
        completed = subprocess.run(
            [str(self.settings.executable), *self.settings.args, "config", "get", "search.mcp_keyword_only"],
            cwd=str(self.settings.home),
            env=client_knowledge_environment(self.settings),
            capture_output=True,
            text=True,
            timeout=self.settings.timeout_seconds,
            check=False,
        )
        if (
            completed.returncode != 0
            or completed.stdout.strip().lower() != "true"
            or "source: db plane" not in completed.stderr
        ):
            raise RuntimeError("GBrain search.mcp_keyword_only must read back as true")

    def search(self, query: str, *, limit: int) -> list[dict[str, Any]]:
        payload = json.dumps({"query": query, "limit": limit}, separators=(",", ":"))
        result = self._run(
            ["call", "--source", self.settings.source_id, "search", payload]
        )
        if not isinstance(result, list):
            raise RuntimeError("pinned GBrain search did not return a list")
        return result

    def get_page(self, slug: str) -> dict[str, Any]:
        payload = json.dumps({"slug": slug, "fuzzy": False}, separators=(",", ":"))
        result = self._run(
            ["call", "--source", self.settings.source_id, "get_page", payload]
        )
        if not isinstance(result, dict):
            raise RuntimeError("pinned GBrain get_page did not return an object")
        return result
