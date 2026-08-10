"""Pinned GBrain subprocess client with an allowlisted environment."""

from __future__ import annotations

import json
import os
import shutil
import stat
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
    source_checkout: Path | None = None
    source_branch: str = "main"
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
    source_checkout_raw = str(block.get("source_checkout") or "").strip()
    source_checkout = Path(source_checkout_raw).expanduser() if source_checkout_raw else None
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
    if source_checkout is not None:
        if not source_checkout.is_absolute():
            raise ClientKnowledgeValidationError(
                "client_knowledge.gbrain.source_checkout must be an absolute path"
            )
        _assert_nonsymlink_directory(source_checkout, "configured GBrain source checkout")
    source_branch = str(block.get("source_branch") or "main").strip()
    if not source_branch or any(ch.isspace() or ord(ch) < 32 for ch in source_branch):
        raise ClientKnowledgeValidationError("GBrain source branch is invalid")
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
        source_checkout=source_checkout,
        source_branch=source_branch,
        args=resolved_args,
        source_id=source_id,
        timeout_seconds=max(1.0, min(timeout, 120.0)),
        max_context_chars=max(1_000, min(max_context, 32_000)),
    )


def _assert_nonsymlink_directory(path: Path, label: str) -> Path:
    """Reject symlink roots/components before resolving filesystem identity."""
    if not path.is_absolute():
        raise ClientKnowledgeValidationError(f"{label} must be absolute")
    if any(part in {".", ".."} for part in path.parts):
        raise ClientKnowledgeValidationError(f"{label} may not contain dot components")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            item = os.lstat(current)
        except OSError as exc:
            raise ClientKnowledgeValidationError(f"{label} does not exist") from exc
        if stat.S_ISLNK(item.st_mode):
            raise ClientKnowledgeValidationError(f"{label} may not contain symlinks")
        if not stat.S_ISDIR(item.st_mode):
            raise ClientKnowledgeValidationError(f"{label} must be a directory")
    return path


def client_knowledge_environment(settings: GBrainSettings) -> dict[str, str]:
    env = {key: os.environ[key] for key in _ENV_ALLOWLIST if os.environ.get(key)}
    path_entries = [str(settings.executable.parent)]
    git = shutil.which("git")
    if git:
        git_parent = str(Path(git).parent)
        if git_parent not in path_entries:
            path_entries.append(git_parent)
    env.update(
        {
            # Pinned GBrain treats GBRAIN_HOME as a parent root and creates
            # ``.gbrain`` below it. Keep generic HOME on that same isolated
            # root so no secondary config/cache path can escape the boundary.
            "HOME": str(settings.home),
            "GBRAIN_HOME": str(settings.home),
            "PATH": os.pathsep.join(path_entries),
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

    def _run(self, args: list[str], *, allow_plain_success: bool = False) -> Any:
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
            if allow_plain_success and stdout.strip():
                return {"status": stdout.strip()}
            raise RuntimeError("pinned GBrain returned malformed JSON") from exc

    def list_sources(self) -> list[dict[str, Any]]:
        result = self._run(["sources", "list", "--json"])
        if isinstance(result, dict):
            result = result.get("sources")
        if not isinstance(result, list) or not all(isinstance(item, dict) for item in result):
            raise RuntimeError("pinned GBrain source list did not return a source array")
        return result

    def assert_source_checkout(self) -> Path:
        configured = self.settings.source_checkout
        if configured is None:
            raise RuntimeError("client knowledge source checkout is not configured")
        _assert_nonsymlink_directory(configured, "configured GBrain source checkout")
        matches = [
            item for item in self.list_sources()
            if str(item.get("id") or item.get("source_id") or "") == self.settings.source_id
        ]
        if len(matches) != 1:
            raise RuntimeError("pinned GBrain must expose exactly one client-knowledge source")
        raw = matches[0].get("local_path")
        if not isinstance(raw, str) or not raw.strip():
            raise RuntimeError("pinned GBrain source is missing local_path")
        listed = Path(raw)
        _assert_nonsymlink_directory(listed, "GBrain source local_path")
        configured_real = Path(os.path.realpath(configured))
        listed_real = Path(os.path.realpath(listed))
        if configured_real != listed_real or not os.path.samefile(configured, listed):
            raise RuntimeError("GBrain source local_path does not match the configured checkout")
        return configured_real

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

    def assert_runtime_ready(self) -> Path:
        """Reprove the pinned executable, checkout, config, and source boundary."""
        self.assert_pinned_version()
        self.assert_pinned_checkout()
        self.assert_keyword_only()
        return self.assert_source_checkout()

    def search(self, query: str, *, limit: int) -> list[dict[str, Any]]:
        self.assert_runtime_ready()
        payload = json.dumps({"query": query, "limit": limit}, separators=(",", ":"))
        result = self._run(
            ["call", "--source", self.settings.source_id, "search", payload]
        )
        if not isinstance(result, list):
            raise RuntimeError("pinned GBrain search did not return a list")
        return result

    def get_page(self, slug: str) -> dict[str, Any]:
        self.assert_runtime_ready()
        payload = json.dumps({"slug": slug, "fuzzy": False}, separators=(",", ":"))
        result = self._run(
            ["call", "--source", self.settings.source_id, "get_page", payload]
        )
        if not isinstance(result, dict):
            raise RuntimeError("pinned GBrain get_page did not return an object")
        return result

    def parse_markdown(
        self,
        content: bytes,
        *,
        file_path: str,
        expected_slug: str,
    ) -> dict[str, Any]:
        """Parse one candidate page with the exact pinned GBrain parser."""
        self.assert_pinned_version()
        self.assert_pinned_checkout()
        try:
            text = bytes(content).decode("utf-8", "strict")
        except UnicodeError as exc:
            raise RuntimeError("candidate GBrain markdown is not UTF-8") from exc
        checkout = self.settings.checkout
        assert checkout is not None
        parser_uri = (checkout / "src" / "core" / "markdown.ts").resolve().as_uri()
        script = (
            f"import {{ parseMarkdown }} from {json.dumps(parser_uri)};"
            "const content = await Bun.stdin.text();"
            f"const parsed = parseMarkdown(content, {json.dumps(file_path)}, "
            f"{{validate:true,expectedSlug:{json.dumps(expected_slug)}}});"
            "console.log(JSON.stringify(parsed));"
        )
        completed = subprocess.run(
            [str(self.settings.executable), "-e", script],
            cwd=str(checkout),
            env=client_knowledge_environment(self.settings),
            input=text,
            capture_output=True,
            text=True,
            timeout=self.settings.timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError("pinned GBrain markdown parser failed")
        if len(completed.stdout) > MAX_RAW_RESULT_CHARS:
            raise RuntimeError("pinned GBrain parser response exceeded the raw result limit")
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("pinned GBrain parser returned malformed JSON") from exc
        if not isinstance(result, dict):
            raise RuntimeError("pinned GBrain parser did not return an object")
        return result

    def sync_no_pull(self) -> dict[str, Any]:
        self.assert_runtime_ready()
        result = self._run(
            [
                "sync", "--source", self.settings.source_id, "--no-pull",
                "--no-embed", "--yes", "--json",
            ],
            allow_plain_success=True,
        )
        if not isinstance(result, dict):
            raise RuntimeError("pinned GBrain sync did not return an object")
        return result
