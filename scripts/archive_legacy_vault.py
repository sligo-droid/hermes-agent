#!/usr/bin/env python3
"""Dry-run-first post-merge cutover for the retired host note system.

Modes are deliberately explicit: preflight, apply, verify, and restore. Apply
requires an exact confirmation string and a merged-code gate. The helper emits
only structural paths, counts, hashes, unit names, and STOP reasons; it never
prints file content, memory text, credentials, or environment values.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable

MARKER = "obsidian"
CONFIRMATION = "RETIRE-OBSIDIAN-POST-MERGE"
RECEIPT_VERSION = 1
RECEIPT_DIR_MODE = 0o700
RECEIPT_FILE_MODE = 0o600
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DEDICATED_SKILL_NAMES = {
    "obsidian",
    "obsidian-cli",
    "obsidian-project-curation",
    "obsidian-memory-system",
}
OBSIDIAN_SOURCED_SKILL_NAMES = {"json-canvas", "defuddle"}
GMAIL_UNITS = (
    "gmail-intake-pubsub.service",
    "gmail-intake-watch-renew.timer",
    "gmail-intake-watch-renew.service",
)
GATEWAY_UNIT = "hermes-gateway.service"
QMD_SKILLS_REFRESH_UNIT = "qmd-skills-refresh.service"
VAULT_RECEIPT_FILES = (
    "MANIFEST.json",
    "SHA256SUMS",
    "FILE_METADATA.tsv",
    "VERIFICATION_RECEIPT.json",
)


class StopCutover(RuntimeError):
    """A fail-closed STOP gate."""


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_fingerprint(path: Path) -> dict[str, Any]:
    st = path.lstat()
    if stat.S_ISREG(st.st_mode):
        digest = sha256_file(path)
        kind = "file"
    elif stat.S_ISLNK(st.st_mode):
        digest = sha256_bytes(os.readlink(path).encode("utf-8", errors="surrogateescape"))
        kind = "symlink"
    elif stat.S_ISDIR(st.st_mode):
        digest = ""
        kind = "directory"
    else:
        raise StopCutover(f"unsupported filesystem object: {path}")
    return {
        "kind": kind,
        "sha256": digest,
        "size": st.st_size if kind != "directory" else 0,
        "mode": stat.S_IMODE(st.st_mode),
        "uid": st.st_uid,
        "gid": st.st_gid,
        "mtime_ns": st.st_mtime_ns,
    }


def _walk_tree(root: Path) -> list[tuple[Path, os.stat_result]]:
    rows: list[tuple[Path, os.stat_result]] = []
    stack = [root]
    while stack:
        current = stack.pop()
        with os.scandir(current) as entries:
            children = sorted(entries, key=lambda entry: entry.name, reverse=True)
        for entry in children:
            child = Path(entry.path)
            st = entry.stat(follow_symlinks=False)
            rows.append((child, st))
            if stat.S_ISDIR(st.st_mode):
                stack.append(child)
    return sorted(rows, key=lambda row: row[0].relative_to(root).as_posix())


def tree_manifest(root: Path) -> dict[str, Any]:
    if root.is_symlink() or not root.is_dir():
        raise StopCutover(f"vault source must be a real directory: {root}")
    entries: list[dict[str, Any]] = []
    counts = {"files": 0, "directories": 1, "symlinks": 0, "bytes": 0}
    root_stat = root.lstat()
    entries.append({"path": ".", **file_fingerprint(root)})
    for path, st in _walk_tree(root):
        rel = path.relative_to(root).as_posix()
        info = file_fingerprint(path)
        if stat.S_ISREG(st.st_mode):
            counts["files"] += 1
            counts["bytes"] += st.st_size
        elif stat.S_ISDIR(st.st_mode):
            counts["directories"] += 1
        elif stat.S_ISLNK(st.st_mode):
            counts["symlinks"] += 1
        entries.append({"path": rel, **info})
    canonical = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    return {
        "root": str(root),
        "counts": counts,
        "tree_hash": sha256_bytes(canonical),
        "entries": entries,
    }


def fsync_path(path: Path) -> None:
    flags = os.O_RDONLY
    if path.is_dir() and hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_tree(root: Path) -> None:
    for path, st in _walk_tree(root):
        if stat.S_ISREG(st.st_mode):
            fsync_path(path)
    directories = [path for path, st in _walk_tree(root) if stat.S_ISDIR(st.st_mode)]
    for path in sorted(directories, key=lambda p: len(p.parts), reverse=True):
        fsync_path(path)
    fsync_path(root)


def atomic_write_bytes(path: Path, data: bytes, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temp_name, mode)
        os.replace(temp_name, path)
        fsync_path(path.parent)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, data: Any, mode: int = RECEIPT_FILE_MODE) -> None:
    atomic_write_bytes(
        path,
        (json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8"),
        mode,
    )


def restore_metadata(path: Path, before: os.stat_result) -> None:
    os.chmod(path, stat.S_IMODE(before.st_mode), follow_symlinks=False)
    try:
        os.chown(path, before.st_uid, before.st_gid, follow_symlinks=False)
    except PermissionError:
        current = path.lstat()
        if (current.st_uid, current.st_gid) != (before.st_uid, before.st_gid):
            raise


def atomic_rewrite_preserving_metadata(path: Path, data: bytes) -> None:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or path.is_symlink():
        raise StopCutover(f"refusing non-regular config file: {path}")
    atomic_write_bytes(path, data, stat.S_IMODE(before.st_mode))
    restore_metadata(path, before)
    fsync_path(path.parent)


def safe_json(path: Path, *, default: Any = None) -> Any:
    if not path.exists():
        return copy.deepcopy(default)
    if path.is_symlink() or not path.is_file():
        raise StopCutover(f"refusing unsafe JSON path: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StopCutover(f"malformed JSON at {path}: {type(exc).__name__}") from exc


def _contains_marker(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_contains_marker(key) or _contains_marker(child) for key, child in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains_marker(child) for child in value)
    return MARKER in str(value).lower()


def scrub_current_memory_text(text: str) -> str:
    """Remove current memory entries that direct use of the retired system."""
    out: list[str] = []
    for paragraph in re.split(r"(\n\s*\n)", text):
        if MARKER not in paragraph.lower():
            out.append(paragraph)
            continue
        lower = paragraph.lower()
        active_terms = (
            "vault", "save", "sync", "write", "read", "query", "search", "source of truth",
            "official", "knowledge", "note", "curat", "qmd", "obsidian_vault",
        )
        if any(term in lower for term in active_terms):
            continue
        out.append(paragraph)
    return "".join(out)


def scrub_env_text(text: str) -> tuple[str, list[str]]:
    """Remove only OBSIDIAN_* assignments without exposing values."""
    removed: list[str] = []
    kept: list[str] = []
    for raw_line in text.splitlines(keepends=True):
        candidate = raw_line.lstrip()
        if candidate.startswith("export "):
            candidate = candidate[len("export "):]
        name = candidate.split("=", 1)[0].strip() if "=" in candidate else ""
        if ENV_NAME_RE.fullmatch(name) and name.startswith("OBSIDIAN_"):
            removed.append(name)
        else:
            kept.append(raw_line)
    return "".join(kept), sorted(set(removed))


def skill_name(skill_dir: Path) -> str:
    skill_md = skill_dir / "SKILL.md"
    fallback = skill_dir.name
    if not skill_md.is_file():
        return fallback
    try:
        head = skill_md.read_text(encoding="utf-8", errors="replace")[:5000]
    except OSError:
        return fallback
    match = re.search(r"(?m)^name:\s*[\"']?([^\n\"']+)", head)
    return match.group(1).strip() if match else fallback


def skill_dirs(skills_root: Path) -> list[Path]:
    if not skills_root.is_dir():
        return []
    result: list[Path] = []
    for skill_md in skills_root.rglob("SKILL.md"):
        relative_parts = skill_md.relative_to(skills_root).parts
        if any(part.startswith(".") or part in {"node_modules", "venv", ".venv"} for part in relative_parts):
            continue
        result.append(skill_md.parent)
    return sorted(set(result))


def is_retired_skill_dir(path: Path, lock_entry: dict[str, Any] | None = None) -> bool:
    name = skill_name(path).lower()
    if MARKER in name or name in DEDICATED_SKILL_NAMES:
        return True
    if name in OBSIDIAN_SOURCED_SKILL_NAMES:
        if lock_entry and _contains_marker(lock_entry):
            return True
        try:
            if _contains_marker((path / "SKILL.md").read_text(encoding="utf-8", errors="replace")[:12000]):
                return True
        except OSError:
            pass
    return False


def _remove_name_from_manifest(text: str, names: set[str]) -> str:
    kept = []
    for line in text.splitlines(keepends=True):
        name = line.split(":", 1)[0].strip()
        if name not in names:
            kept.append(line)
    return "".join(kept)


def _remove_names_from_mapping(mapping: Any, names: set[str]) -> Any:
    if isinstance(mapping, dict):
        return {
            key: _remove_names_from_mapping(value, names)
            for key, value in mapping.items()
            if str(key) not in names
        }
    if isinstance(mapping, list):
        return [
            _remove_names_from_mapping(item, names)
            for item in mapping
            if not (isinstance(item, str) and item in names)
            and not (isinstance(item, dict) and str(item.get("name", "")) in names)
        ]
    return mapping


def _remove_retired_taps(data: Any) -> Any:
    if not isinstance(data, dict) or not isinstance(data.get("taps"), list):
        return data
    return {**data, "taps": [tap for tap in data["taps"] if not _contains_marker(tap)]}


@dataclass
class HostPaths:
    home: Path
    systemd_dir: Path
    archive_root: Path
    canonical_repo: Path
    receipt_root: Path
    profile_homes: list[Path]


class CommandRunner:
    """Small command boundary so tests can mock systemd/QMD/gateway/Honcho."""

    def run(self, args: list[str], *, timeout: int = 60, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            args,
            text=True,
            capture_output=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            check=False,
        )
        if check and result.returncode != 0:
            raise StopCutover(f"command failed ({args[0]} {args[1] if len(args) > 1 else ''}): exit {result.returncode}")
        return result

    def systemctl(self, *args: str, check: bool = True) -> str:
        return self.run(["systemctl", "--user", *args], timeout=240, check=check).stdout.strip()

    def qmd(self, *args: str, check: bool = True) -> str:
        return self.run(["qmd", *args], timeout=900, check=check).stdout.strip()

    def git(self, repo: Path, *args: str, check: bool = True) -> str:
        return self.run(["git", "-C", str(repo), *args], timeout=120, check=check).stdout.strip()


class HonchoBackend:
    """Optional Honcho mutation boundary; the SDK is imported only during apply."""

    def inventory(self, config_path: Path, host: str) -> dict[str, Any]:
        from plugins.memory.honcho.client import HonchoClientConfig, get_honcho_client, reset_honcho_client

        config = HonchoClientConfig.from_global_config(host=host, config_path=config_path)
        if not config.enabled:
            return {"enabled": False, "host": host, "workspace": config.workspace_id, "scopes": []}
        reset_honcho_client()
        client = get_honcho_client(config)
        peers: list[str] = []
        page = 1
        while True:
            result = client.peers(page=page, size=100)
            items = list(getattr(result, "items", []) or [])
            peers.extend(str(getattr(item, "id", item)) for item in items)
            if len(items) < 100:
                break
            page += 1
        scopes = [{"observer": observer, "target": target} for observer in peers for target in peers]
        return {
            "enabled": True,
            "host": host,
            "workspace": config.workspace_id,
            "peer_count": len(peers),
            "scopes": scopes,
        }

    @staticmethod
    def _all_conclusions(scope: Any) -> list[Any]:
        conclusions: list[Any] = []
        page = 1
        while True:
            result = scope.list(page=page, size=100)
            items = list(getattr(result, "items", []) or [])
            conclusions.extend(items)
            if len(items) < 100:
                break
            page += 1
        return conclusions

    def scrub(self, config_path: Path, host: str) -> dict[str, Any]:
        from plugins.memory.honcho.client import HonchoClientConfig, get_honcho_client, reset_honcho_client

        config = HonchoClientConfig.from_global_config(host=host, config_path=config_path)
        if not config.enabled:
            return {"host": host, "changed_cards": [], "deleted_conclusions": [], "remaining_search_hits": 0}
        reset_honcho_client()
        client = get_honcho_client(config)
        peers: list[str] = []
        page = 1
        while True:
            result = client.peers(page=page, size=100)
            items = list(getattr(result, "items", []) or [])
            peers.extend(str(getattr(item, "id", item)) for item in items)
            if len(items) < 100:
                break
            page += 1
        changed_cards: list[dict[str, Any]] = []
        deleted: list[dict[str, Any]] = []
        context_scopes: list[dict[str, Any]] = []
        for observer_id in peers:
            observer = client.peer(observer_id)
            for target_id in peers:
                card = list(observer.get_card(target=target_id) or [])
                clean_card = [fact for fact in card if MARKER not in str(fact).lower()]
                if clean_card != card:
                    observer.set_card(clean_card, target=target_id)
                    changed_cards.append({
                        "observer": observer_id,
                        "target": target_id,
                        "before": card,
                        "after": clean_card,
                    })
                scope = observer.conclusions_of(target_id)
                for conclusion in self._all_conclusions(scope):
                    if MARKER in str(getattr(conclusion, "content", "")).lower():
                        conclusion_id = str(getattr(conclusion, "id", ""))
                        if not conclusion_id:
                            raise StopCutover("Honcho conclusion lacks an id; refusing partial cleanup")
                        deleted.append({
                            "observer": observer_id,
                            "target": target_id,
                            "id": conclusion_id,
                            "content": str(getattr(conclusion, "content", "")),
                            "session_id": str(getattr(conclusion, "session_id", "")),
                        })
                        scope.delete(conclusion_id)
                context = observer.context(target=target_id, search_query=MARKER, search_top_k=20)
                context_scopes.append({
                    "observer": observer_id,
                    "target": target_id,
                    "marker_present": MARKER in str(context).lower(),
                })
        hits = client.search(MARKER, limit=100)
        remaining = sum(1 for hit in hits if MARKER in str(getattr(hit, "content", "")).lower())
        return {
            "host": host,
            "changed_cards": changed_cards,
            "deleted_conclusions": deleted,
            "context_scopes": context_scopes,
            "remaining_search_hits": remaining,
            "messages_preserved": True,
            "sessions_preserved": True,
        }

    def restore(self, config_path: Path, host: str, snapshot: dict[str, Any]) -> None:
        from plugins.memory.honcho.client import HonchoClientConfig, get_honcho_client, reset_honcho_client

        config = HonchoClientConfig.from_global_config(host=host, config_path=config_path)
        if not config.enabled:
            return
        reset_honcho_client()
        client = get_honcho_client(config)
        for changed in snapshot.get("changed_cards", []):
            observer = client.peer(changed["observer"])
            current = list(observer.get_card(target=changed["target"]) or [])
            if current != changed["after"]:
                raise StopCutover("concurrent Honcho card drift; refusing restore")
            observer.set_card(changed["before"], target=changed["target"])
        grouped: dict[tuple[str, str], list[dict[str, str]]] = {}
        for conclusion in snapshot.get("deleted_conclusions", []):
            grouped.setdefault((conclusion["observer"], conclusion["target"]), []).append({
                "content": conclusion["content"],
                "session_id": conclusion.get("session_id") or None,
            })
        for (observer_id, target_id), conclusions in grouped.items():
            client.peer(observer_id).conclusions_of(target_id).create(conclusions)

    def assert_unchanged(self, config_path: Path, host: str, snapshot: dict[str, Any]) -> None:
        """Fail if receipt-owned Honcho cards/conclusions have drifted."""
        from plugins.memory.honcho.client import HonchoClientConfig, get_honcho_client, reset_honcho_client

        config = HonchoClientConfig.from_global_config(host=host, config_path=config_path)
        if not config.enabled:
            return
        reset_honcho_client()
        client = get_honcho_client(config)
        for changed in snapshot.get("changed_cards", []):
            current = list(client.peer(changed["observer"]).get_card(target=changed["target"]) or [])
            if current != changed["after"]:
                raise StopCutover("concurrent Honcho card drift; refusing restore")
        for deleted in snapshot.get("deleted_conclusions", []):
            items = self._all_conclusions(
                client.peer(deleted["observer"]).conclusions_of(deleted["target"])
            )
            if any(str(item.id) == deleted["id"] for item in items):
                raise StopCutover("concurrent Honcho conclusion drift; refusing restore")


class CutoverController:
    def __init__(
        self,
        paths: HostPaths,
        runner: CommandRunner | None = None,
        honcho: HonchoBackend | None = None,
        now: Callable[[], str] = utc_timestamp,
        fault_after_step: str = "",
    ) -> None:
        self.paths = paths
        self.runner = runner or CommandRunner()
        self.honcho = honcho or HonchoBackend()
        self.now = now
        self.fault_after_step = fault_after_step

    def _fault(self, step: str) -> None:
        if self.fault_after_step == step:
            raise RuntimeError(f"injected crash after {step}")

    def _receipt_dir(self, receipt_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", receipt_id):
            raise StopCutover("invalid receipt id")
        path = self.paths.receipt_root / receipt_id
        try:
            path.resolve().relative_to(self.paths.receipt_root.resolve())
        except ValueError as exc:
            raise StopCutover("receipt path escapes receipt root") from exc
        return path

    def _config_paths(self) -> list[Path]:
        candidates = [self.paths.home / "honcho.json", Path.home() / ".honcho/config.json"]
        candidates.extend(profile / "honcho.json" for profile in self.paths.profile_homes)
        return sorted({path for path in candidates if path.exists()})

    def _host_inventory(self) -> list[dict[str, Any]]:
        inventory: list[dict[str, Any]] = []
        for config_path in self._config_paths():
            raw = safe_json(config_path, default={})
            if not isinstance(raw, dict):
                raise StopCutover(f"Honcho config must be an object: {config_path}")
            hosts = raw.get("hosts") or {}
            if not isinstance(hosts, dict):
                raise StopCutover(f"Honcho hosts map must be an object: {config_path}")
            selectable = set(str(host) for host in hosts)
            if config_path == self.paths.home / "honcho.json":
                selectable.add("hermes")
                selectable.update(f"hermes_{profile.name}" for profile in self.paths.profile_homes)
            for profile in self.paths.profile_homes:
                if config_path == profile / "honcho.json":
                    selectable.add(f"hermes_{profile.name}")
            default_host = str(raw.get("defaultHost", "")).strip()
            if default_host:
                selectable.add(default_host)
            if not selectable:
                selectable.add("hermes")
            for host in sorted(selectable):
                block = hosts.get(host, {}) if isinstance(hosts.get(host, {}), dict) else {}
                enabled = block.get("enabled", raw.get("enabled"))
                has_connection = bool(
                    block.get("apiKey") or raw.get("apiKey") or raw.get("baseUrl") or raw.get("base_url")
                )
                member = host in hosts or not hosts
                selected = member and (enabled is True or (enabled is None and has_connection))
                inventory.append({
                    "config_path": str(config_path),
                    "host": host,
                    "selected": selected,
                    "explicitly_disabled": enabled is False,
                })
        return inventory

    def _ensure_unknown_hosts_closed(self, receipt_dir: Path) -> list[dict[str, Any]]:
        changed: list[dict[str, Any]] = []
        for config_path in self._config_paths():
            raw = safe_json(config_path, default={})
            hosts = raw.get("hosts") or {}
            required_hosts: set[str] = set()
            if config_path == self.paths.home / "honcho.json":
                required_hosts.add("hermes")
                required_hosts.update(f"hermes_{profile.name}" for profile in self.paths.profile_homes)
            for profile in self.paths.profile_homes:
                if config_path == profile / "honcho.json":
                    required_hosts.add(f"hermes_{profile.name}")
            new_hosts = dict(hosts) if isinstance(hosts, dict) else {}
            if not new_hosts:
                # A flat config can auto-select arbitrary HERMES_HONCHO_HOST
                # values. Materialize the default host membership to close it.
                new_hosts[str(raw.get("defaultHost") or "hermes")] = {}
            for required_host in required_hosts:
                new_hosts.setdefault(required_host, {})
            if new_hosts != hosts:
                raw["hosts"] = new_hosts
                backup = receipt_dir / "files-backup" / sha256_bytes(str(config_path).encode())[:12]
                backup.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_bytes(backup, config_path.read_bytes(), RECEIPT_FILE_MODE)
                before = file_fingerprint(config_path)
                atomic_rewrite_preserving_metadata(
                    config_path,
                    (json.dumps(raw, indent=2, sort_keys=True) + "\n").encode(),
                )
                changed.append({
                    "path": str(config_path),
                    "backup": str(backup),
                    "before": before,
                    "after": file_fingerprint(config_path),
                })
        return changed

    def _repo_gate(self, expected_merge_sha: str) -> dict[str, Any]:
        head = self.runner.git(self.paths.canonical_repo, "rev-parse", "HEAD")
        status = self.runner.git(self.paths.canonical_repo, "status", "--porcelain")
        # git merge-base emits no stdout; use a direct run for its exit status.
        ancestor_result = self.runner.run(
            ["git", "-C", str(self.paths.canonical_repo), "merge-base", "--is-ancestor", expected_merge_sha, "HEAD"],
            check=False,
        )
        if status:
            raise StopCutover("canonical checkout is dirty")
        if ancestor_result.returncode != 0:
            raise StopCutover("expected merged removal SHA is not in canonical HEAD")
        grep_result = self.runner.run(
            [
                "git", "-C", str(self.paths.canonical_repo), "grep", "-Il", "obsidian-vault", "--",
                "*.py", "*.sh", "*.service", "*.timer",
            ],
            check=False,
        )
        live_python_hits = [
            path.strip()
            for path in grep_result.stdout.splitlines()
            if path.strip()
            and path.strip() != "scripts/archive_legacy_vault.py"
            and not path.strip().startswith("tests/")
        ]
        if live_python_hits:
            raise StopCutover(
                f"merged executable code still names the legacy vault ({len(live_python_hits)} files)"
            )
        return {
            "head": head,
            "expected_merge_sha": expected_merge_sha,
            "clean": True,
            "live_vault_access_files": 0,
        }

    def _vault_open_handle_gate(self, source: Path) -> None:
        result = self.runner.run(["lsof", "+D", str(source)], timeout=120, check=False)
        if result.returncode == 0 and len(result.stdout.splitlines()) > 1:
            raise StopCutover("a live process still has the legacy vault open")

    def _gateway_idle_gate(self) -> dict[str, Any]:
        active_turns = 0
        development_workers: list[str] = []
        state_path = self.paths.home / "gateway_state.json"
        if state_path.exists():
            state = safe_json(state_path, default={})
            active_turns += int(state.get("active_turns", 0) or 0) if isinstance(state, dict) else 0
        processes_path = self.paths.home / "processes.json"
        if processes_path.exists():
            processes = safe_json(processes_path, default={})
            if _contains_marker(processes):
                # This is structural detection only; never include process command content.
                development_workers.append("retired-marker-process")
        units = self.runner.systemctl(
            "list-units", "--state=running", "--no-legend", "--plain", check=False
        )
        for line in units.splitlines():
            unit = line.split(None, 1)[0] if line.strip() else ""
            if any(token in unit for token in ("hermes-kanban-worker", "claw-dev@", "paseo-agent")):
                development_workers.append(unit)
        process_result = self.runner.run(
            ["pgrep", "-af", "(?:opencode|codex|claude|paseo.*agent)"],
            check=False,
        )
        for line in process_result.stdout.splitlines():
            pid_text = line.split(None, 1)[0] if line.strip() else ""
            if pid_text.isdigit() and int(pid_text) not in {os.getpid(), os.getppid()}:
                development_workers.append("development-process")
        if active_turns or development_workers:
            raise StopCutover(
                f"gateway/development work is active (turns={active_turns}, workers={len(development_workers)})"
            )
        return {"active_turns": active_turns, "development_workers": []}

    def _vault_destination(self, stamp: str) -> Path:
        return self.paths.archive_root / f"obsidian-vault-{stamp}" / "vault"

    def _vault_preflight(self, stamp: str) -> dict[str, Any]:
        source = self.paths.home / "obsidian-vault"
        if source.is_symlink() or not source.is_dir():
            raise StopCutover(f"vault source missing or unsafe: {source}")
        archive_root = self.paths.archive_root
        if not archive_root.exists():
            existing_ancestor = archive_root.parent
            while not existing_ancestor.exists() and existing_ancestor != existing_ancestor.parent:
                existing_ancestor = existing_ancestor.parent
            if not existing_ancestor.is_dir():
                raise StopCutover(f"archive path has no existing directory ancestor: {archive_root}")
            destination_device = existing_ancestor.stat().st_dev
        else:
            destination_device = archive_root.stat().st_dev
        source_device = source.stat().st_dev
        if source_device != destination_device:
            raise StopCutover("vault archive destination is not on the source filesystem")
        destination = self._vault_destination(stamp)
        if destination.exists() or destination.parent.exists():
            raise StopCutover(f"archive destination already exists: {destination.parent}")
        return {
            "source": str(source),
            "destination": str(destination),
            "device": source_device,
            "manifest": tree_manifest(source),
        }

    def _write_vault_receipts(self, vault: Path, before: dict[str, Any], after: dict[str, Any]) -> None:
        manifest = {"version": RECEIPT_VERSION, "before": before, "after": after}
        atomic_write_json(vault / "MANIFEST.json", manifest)
        sums = []
        metadata = ["path\tkind\tsize\tmode\tuid\tgid\tmtime_ns\tsha256"]
        for entry in after["entries"]:
            if entry["kind"] == "file" and entry["path"] not in VAULT_RECEIPT_FILES:
                sums.append(f"{entry['sha256']}  {entry['path']}")
            metadata.append(
                "\t".join(str(entry[key]) for key in ("path", "kind", "size", "mode", "uid", "gid", "mtime_ns", "sha256"))
            )
        atomic_write_bytes(vault / "SHA256SUMS", ("\n".join(sums) + "\n").encode(), RECEIPT_FILE_MODE)
        atomic_write_bytes(vault / "FILE_METADATA.tsv", ("\n".join(metadata) + "\n").encode(), RECEIPT_FILE_MODE)
        fsync_tree(vault)
        final = tree_manifest(vault)
        atomic_write_json(vault / "VERIFICATION_RECEIPT.json", {
            "version": RECEIPT_VERSION,
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "payload_tree_hash": after["tree_hash"],
            "final_tree_hash": final["tree_hash"],
            "counts": after["counts"],
        })
        fsync_tree(vault)
        for directory in (vault.parent, vault.parent.parent, vault.parent.parent.parent):
            if directory.is_dir():
                fsync_path(directory)

    def _archive_vault(self, vault_plan: dict[str, Any]) -> dict[str, Any]:
        source = Path(vault_plan["source"])
        destination = Path(vault_plan["destination"])
        self._vault_open_handle_gate(source)
        destination.parent.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(destination.parent.parent, 0o700)
        if source.stat().st_dev != destination.parent.parent.stat().st_dev:
            raise StopCutover("device changed after preflight")
        destination.parent.mkdir(mode=0o700)
        before = tree_manifest(source)
        if before["tree_hash"] != vault_plan["manifest"]["tree_hash"]:
            raise StopCutover("vault changed after preflight")
        os.rename(source, destination)
        fsync_path(source.parent)
        fsync_path(destination.parent)
        try:
            self._fault("vault-rename")
            after = tree_manifest(destination)
            if before["tree_hash"] != after["tree_hash"]:
                raise StopCutover("vault changed during atomic rename")
            self._write_vault_receipts(destination, before, after)
        except BaseException:
            if not source.exists() and destination.exists():
                os.rename(destination, source)
                fsync_path(source.parent)
            raise
        if source.exists() or source.is_symlink():
            raise StopCutover("vault source still exists after archive")
        return {
            "source": str(source),
            "destination": str(destination),
            "tree_hash": before["tree_hash"],
            "counts": before["counts"],
        }

    def _skill_roots(self) -> list[Path]:
        return [self.paths.home / "skills", *[home / "skills" for home in self.paths.profile_homes]]

    def _plan_skill_reconcile(self) -> list[dict[str, Any]]:
        planned: list[dict[str, Any]] = []
        for root in self._skill_roots():
            lock_path = root / ".hub/lock.json"
            lock = safe_json(lock_path, default={"installed": {}}) if lock_path.exists() else {"installed": {}}
            installed = lock.get("installed", {}) if isinstance(lock, dict) else {}
            for path in skill_dirs(root):
                name = skill_name(path)
                entry = installed.get(name) if isinstance(installed, dict) else None
                if is_retired_skill_dir(path, entry if isinstance(entry, dict) else None):
                    planned.append({"root": str(root), "path": str(path), "name": name})
        return planned

    def _apply_skill_reconcile(self, receipt_dir: Path, planned: list[dict[str, Any]]) -> dict[str, Any]:
        backup_root = receipt_dir / "skills-backup"
        backup_root.mkdir(mode=0o700)
        removed_names_by_root: dict[str, set[str]] = {}
        moved: list[dict[str, str]] = []
        journal_path = receipt_dir / "SKILLS_JOURNAL.json"
        journal: dict[str, Any] = {"moved": moved, "changed_files": []}
        atomic_write_json(journal_path, journal)
        for item in planned:
            root = Path(item["root"])
            source = Path(item["path"])
            if not source.exists():
                raise StopCutover(f"planned skill path drifted: {source}")
            relative = source.relative_to(root)
            backup = backup_root / sha256_bytes(str(root).encode())[:12] / relative
            backup.parent.mkdir(parents=True, exist_ok=True)
            os.rename(source, backup)
            moved.append({"source": str(source), "backup": str(backup), "root": str(root), "name": item["name"]})
            atomic_write_json(journal_path, journal)
            removed_names_by_root.setdefault(str(root), set()).add(item["name"])
        self._fault("skills-moved")
        changed_files: list[dict[str, Any]] = []
        for root_text, names in removed_names_by_root.items():
            root = Path(root_text)
            candidates = [
                root / ".hub/lock.json",
                root / ".usage.json",
                root / ".bundled_manifest",
                root / ".hub/taps.json",
            ]
            for path in candidates:
                if not path.exists():
                    continue
                before_bytes = path.read_bytes()
                before = file_fingerprint(path)
                if path.name == ".bundled_manifest":
                    after_bytes = _remove_name_from_manifest(before_bytes.decode("utf-8"), names).encode()
                else:
                    parsed = safe_json(path)
                    if path.name == "lock.json" and isinstance(parsed, dict):
                        parsed["installed"] = _remove_names_from_mapping(parsed.get("installed", {}), names)
                    elif path.name == "taps.json":
                        parsed = _remove_retired_taps(parsed)
                    else:
                        parsed = _remove_names_from_mapping(parsed, names)
                    after_bytes = (json.dumps(parsed, indent=2, sort_keys=True) + "\n").encode()
                if after_bytes != before_bytes:
                    backup = receipt_dir / "files-backup" / sha256_bytes(str(path).encode())[:12]
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    atomic_write_bytes(backup, before_bytes, RECEIPT_FILE_MODE)
                    atomic_rewrite_preserving_metadata(path, after_bytes)
                    changed_files.append({
                        "path": str(path), "backup": str(backup), "before": before, "after": file_fingerprint(path),
                    })
                    journal["changed_files"] = changed_files
                    atomic_write_json(journal_path, journal)
            snapshot = root.parent / ".skills_prompt_snapshot.json"
            if snapshot.exists():
                before = file_fingerprint(snapshot)
                backup = receipt_dir / "files-backup" / sha256_bytes(str(snapshot).encode())[:12]
                backup.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_bytes(backup, snapshot.read_bytes(), RECEIPT_FILE_MODE)
                snapshot.unlink()
                changed_files.append({"path": str(snapshot), "backup": str(backup), "before": before, "after": None})
                journal["changed_files"] = changed_files
                atomic_write_json(journal_path, journal)
            for cache in (root / ".hub/index-cache", root / "index-cache"):
                if cache.exists():
                    backup = backup_root / sha256_bytes(str(root).encode())[:12] / cache.relative_to(root)
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    os.rename(cache, backup)
                    moved.append({"source": str(cache), "backup": str(backup), "root": str(root), "name": "index-cache"})
                    atomic_write_json(journal_path, journal)
        return {"moved": moved, "changed_files": changed_files}

    def _apply_memory_env(self, receipt_dir: Path) -> dict[str, Any]:
        changed: list[dict[str, Any]] = []
        homes = [self.paths.home, *self.paths.profile_homes]
        for home in homes:
            for memory_name in ("MEMORY.md", "USER.md"):
                path = home / "memories" / memory_name
                if not path.exists():
                    continue
                before_bytes = path.read_bytes()
                after_text = scrub_current_memory_text(before_bytes.decode("utf-8", errors="strict"))
                after_bytes = after_text.encode()
                if after_bytes != before_bytes:
                    backup = receipt_dir / "files-backup" / sha256_bytes(str(path).encode())[:12]
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    atomic_write_bytes(backup, before_bytes, RECEIPT_FILE_MODE)
                    before = file_fingerprint(path)
                    atomic_rewrite_preserving_metadata(path, after_bytes)
                    changed.append({"path": str(path), "backup": str(backup), "before": before, "after": file_fingerprint(path), "kind": "memory"})
            for env_path in (home / ".env", home / "gmail-intake.env"):
                if not env_path.exists():
                    continue
                before_bytes = env_path.read_bytes()
                after_text, removed_names = scrub_env_text(before_bytes.decode("utf-8", errors="strict"))
                if removed_names:
                    backup = receipt_dir / "files-backup" / sha256_bytes(str(env_path).encode())[:12]
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    atomic_write_bytes(backup, before_bytes, RECEIPT_FILE_MODE)
                    before = file_fingerprint(env_path)
                    atomic_rewrite_preserving_metadata(env_path, after_text.encode())
                    changed.append({"path": str(env_path), "backup": str(backup), "before": before, "after": file_fingerprint(env_path), "kind": "environment", "removed_names": removed_names})
        return {"changed_files": changed}

    def _neutralize_invoke_agent(self, receipt_dir: Path) -> dict[str, Any]:
        script = self.paths.home / "scripts/gmail-intake/gmail_intake_collector.py"
        if not script.exists():
            return {"changed_files": []}
        before_bytes = script.read_bytes()
        text = before_bytes.decode("utf-8", errors="strict")
        patterns = [
            (r"default=os\.environ\.get\(\"GMAIL_INTAKE_INVOKE_AGENT\"\) == \"1\"", "default=False"),
            (r"if result\[\"gated\"\] and args\.invoke_agent:", "if False and args.invoke_agent:"),
            (r"if result\[\"gated\"\] and invoke_agent_flag:", "if False and invoke_agent_flag:"),
        ]
        changed_text = text
        for pattern, replacement in patterns:
            changed_text = re.sub(pattern, replacement, changed_text)
        if changed_text == text:
            raise StopCutover("legacy Gmail collector exists but invoke-agent seam could not be neutralized")
        backup = receipt_dir / "files-backup" / sha256_bytes(str(script).encode())[:12]
        backup.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(backup, before_bytes, RECEIPT_FILE_MODE)
        before = file_fingerprint(script)
        atomic_rewrite_preserving_metadata(script, changed_text.encode())
        return {"changed_files": [{"path": str(script), "backup": str(backup), "before": before, "after": file_fingerprint(script), "kind": "invoke-agent"}]}

    def _stop_legacy_gmail(self) -> dict[str, Any]:
        before = {unit: {
            "active": self.runner.systemctl("is-active", unit, check=False),
            "enabled": self.runner.systemctl("is-enabled", unit, check=False),
        } for unit in GMAIL_UNITS}
        self.runner.systemctl("disable", "--now", "gmail-intake-pubsub.service", check=False)
        self.runner.systemctl("disable", "--now", "gmail-intake-watch-renew.timer", check=False)
        self.runner.systemctl("stop", "gmail-intake-watch-renew.service", check=False)
        after = {unit: {
            "active": self.runner.systemctl("is-active", unit, check=False),
            "enabled": self.runner.systemctl("is-enabled", unit, check=False),
        } for unit in GMAIL_UNITS}
        if any(state["active"] in {"active", "activating"} for state in after.values()):
            raise StopCutover("legacy Gmail collector or renewal service remains active")
        return {"before": before, "after": after}

    def _qmd_refresh(self) -> dict[str, Any]:
        # Prefer the host's established generator/service. Fallback targets only
        # the skills index and never invokes or modifies pid-docs.
        service_result = self.runner.systemctl("start", QMD_SKILLS_REFRESH_UNIT, check=False)
        status = self.runner.systemctl("is-failed", QMD_SKILLS_REFRESH_UNIT, check=False)
        fallback = False
        if status == "failed":
            fallback = True
            generator = Path.home() / ".local/bin/qmd-skills-catalog"
            if generator.exists():
                self.runner.run([str(generator)], timeout=300)
            self.runner.qmd("--index", "skills", "update")
            self.runner.qmd("--index", "skills", "embed", "--max-docs-per-batch", "50", "--max-batch-mb", "2")
        qmd_status = self.runner.qmd("--index", "skills", "status")
        if MARKER in qmd_status.lower():
            raise StopCutover("QMD skills status still names the retired system")
        return {"fallback": fallback, "service_output_present": bool(service_result), "verified": True}

    def preflight(self, expected_merge_sha: str = "", require_merged: bool = False) -> dict[str, Any]:
        stamp = self.now()
        hosts = self._host_inventory()
        selected_hosts = [host for host in hosts if host["selected"]]
        scopes = []
        for host in selected_hosts:
            scopes.append(self.honcho.inventory(Path(host["config_path"]), host["host"]))
        result = {
            "mode": "preflight",
            "timestamp": stamp,
            "receipt_root": str(self.paths.receipt_root),
            "host_inventory": hosts,
            "honcho_scopes": scopes,
            "skill_plan": self._plan_skill_reconcile(),
            "vault": self._vault_preflight(stamp),
            "gmail_units": list(GMAIL_UNITS),
            "qmd_index": "skills",
            "pid_docs_touched": False,
            "stop_gates": [],
        }
        if require_merged:
            if not expected_merge_sha:
                raise StopCutover("apply requires --expected-merge-sha")
            result["repo"] = self._repo_gate(expected_merge_sha)
            result["gateway_idle"] = self._gateway_idle_gate()
        return result

    def apply(self, expected_merge_sha: str, confirmation: str) -> dict[str, Any]:
        if confirmation != CONFIRMATION:
            raise StopCutover(f"apply requires --confirm {CONFIRMATION}")
        plan = self.preflight(expected_merge_sha, require_merged=True)
        receipt_id = f"cutover-{plan['timestamp']}-{expected_merge_sha[:12]}"
        receipt_dir = self._receipt_dir(receipt_id)
        self.paths.receipt_root.mkdir(parents=True, exist_ok=True, mode=RECEIPT_DIR_MODE)
        os.chmod(self.paths.receipt_root, RECEIPT_DIR_MODE)
        receipt_dir.mkdir(parents=True, mode=RECEIPT_DIR_MODE)
        os.chmod(receipt_dir, RECEIPT_DIR_MODE)
        atomic_write_json(receipt_dir / "PRECHECK.json", plan)
        receipt: dict[str, Any] = {
            "version": RECEIPT_VERSION,
            "receipt_id": receipt_id,
            "state": "applying",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "expected_merge_sha": expected_merge_sha,
            "plan": plan,
            "steps": {},
        }
        atomic_write_json(receipt_dir / "RECEIPT.json", receipt)
        try:
            receipt["steps"]["host_allowlist"] = self._ensure_unknown_hosts_closed(receipt_dir)
            receipt["steps"]["honcho"] = []
            for host in plan["host_inventory"]:
                if not host["selected"]:
                    continue
                receipt["steps"]["honcho"].append(
                    self.honcho.scrub(Path(host["config_path"]), host["host"])
                )
                atomic_write_json(receipt_dir / "RECEIPT.json", receipt)
            receipt["steps"]["gmail"] = self._stop_legacy_gmail()
            receipt["steps"]["invoke_agent"] = self._neutralize_invoke_agent(receipt_dir)
            receipt["steps"]["skills"] = self._apply_skill_reconcile(receipt_dir, plan["skill_plan"])
            receipt["steps"]["memory_env"] = self._apply_memory_env(receipt_dir)
            self._fault("host-files")
            receipt["steps"]["qmd"] = self._qmd_refresh()
            # Recheck idle immediately before the only gateway restart.
            self._gateway_idle_gate()
            self.runner.systemctl("restart", GATEWAY_UNIT)
            if self.runner.systemctl("is-active", GATEWAY_UNIT, check=False) != "active":
                raise StopCutover("gateway did not become active after restart")
            receipt["steps"]["gateway"] = {"restarted": True, "active": True}
            if len(receipt["steps"]["honcho"]) != sum(
                1 for host in plan["host_inventory"] if host["selected"]
            ):
                raise StopCutover("all-host Honcho receipt is incomplete; refusing vault archive")
            receipt["steps"]["vault"] = {
                "source": plan["vault"]["source"],
                "destination": plan["vault"]["destination"],
                "tree_hash": plan["vault"]["manifest"]["tree_hash"],
                "counts": plan["vault"]["manifest"]["counts"],
                "state": "pending",
            }
            atomic_write_json(receipt_dir / "RECEIPT.json", receipt)
            receipt["steps"]["vault"] = self._archive_vault(plan["vault"])
            self._fault("vault-archived")
            receipt["state"] = "applied"
            receipt["completed_at"] = datetime.now(timezone.utc).isoformat()
            atomic_write_json(receipt_dir / "RECEIPT.json", receipt)
            verification = self.verify(receipt_id)
            receipt["state"] = "verified"
            receipt["verification"] = verification
            atomic_write_json(receipt_dir / "RECEIPT.json", receipt)
            return {"receipt_id": receipt_id, "receipt_dir": str(receipt_dir), "state": "verified"}
        except BaseException as exc:
            receipt["state"] = "failed"
            receipt["failure_class"] = type(exc).__name__
            atomic_write_json(receipt_dir / "RECEIPT.json", receipt)
            try:
                self.restore(receipt_id, automatic=True)
            except BaseException as restore_exc:
                receipt["rollback_failure_class"] = type(restore_exc).__name__
                atomic_write_json(receipt_dir / "RECEIPT.json", receipt)
            raise

    def _load_receipt(self, receipt_id: str) -> tuple[Path, dict[str, Any]]:
        receipt_dir = self._receipt_dir(receipt_id)
        receipt = safe_json(receipt_dir / "RECEIPT.json")
        if not isinstance(receipt, dict):
            raise StopCutover("receipt is malformed")
        return receipt_dir, receipt

    def verify(self, receipt_id: str) -> dict[str, Any]:
        receipt_dir, receipt = self._load_receipt(receipt_id)
        if receipt.get("state") not in {"applied", "verified"}:
            raise StopCutover(f"receipt state is not verifiable: {receipt.get('state')}")
        vault = receipt.get("steps", {}).get("vault", {})
        source = Path(vault.get("source", ""))
        destination = Path(vault.get("destination", ""))
        if source.exists() or source.is_symlink():
            raise StopCutover("vault source was recreated")
        if not destination.is_dir() or destination.is_symlink():
            raise StopCutover("archive destination is missing or unsafe")
        for filename in VAULT_RECEIPT_FILES:
            if not (destination / filename).is_file():
                raise StopCutover(f"archive receipt missing: {filename}")
        manifest = safe_json(destination / "MANIFEST.json")
        payload = dict(manifest["after"])
        # The four receipt files were added after the payload manifest. Verify
        # each payload entry directly and then the verification receipt hash.
        for entry in payload["entries"]:
            path = destination if entry["path"] == "." else destination / PurePosixPath(entry["path"])
            actual = file_fingerprint(path)
            keys = ("kind",) if entry["path"] == "." else ("kind", "sha256", "size", "mode", "uid", "gid", "mtime_ns")
            for key in keys:
                if actual[key] != entry[key]:
                    raise StopCutover(f"archive verification drift at {entry['path']} ({key})")
        verification_receipt = safe_json(destination / "VERIFICATION_RECEIPT.json")
        if verification_receipt.get("payload_tree_hash") != payload.get("tree_hash"):
            raise StopCutover("archive verification receipt does not match payload manifest")
        gmail = {
            unit: self.runner.systemctl("is-active", unit, check=False)
            for unit in GMAIL_UNITS
        }
        if any(value in {"active", "activating"} for value in gmail.values()):
            raise StopCutover("legacy Gmail collector remains active")
        if self.runner.systemctl("is-active", GATEWAY_UNIT, check=False) != "active":
            raise StopCutover("gateway is not active")
        # Controlled structural interaction: load repo state and skill plan;
        # neither may recreate the vault path.
        self._plan_skill_reconcile()
        if source.exists() or source.is_symlink():
            raise StopCutover("controlled verification recreated the vault")
        verification = {
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "receipt_dir": str(receipt_dir),
            "vault_tree_hash": payload["tree_hash"],
            "gmail_units": gmail,
            "gateway_active": True,
            "source_absent": True,
            "pid_docs_touched": False,
        }
        atomic_write_json(receipt_dir / "VERIFY.json", verification)
        return verification

    def restore(self, receipt_id: str, automatic: bool = False) -> dict[str, Any]:
        receipt_dir, receipt = self._load_receipt(receipt_id)
        if receipt.get("state") == "restored":
            return {"receipt_id": receipt_id, "state": "restored", "idempotent": True}
        steps = receipt.get("steps", {})
        journal_path = receipt_dir / "SKILLS_JOURNAL.json"
        if "skills" not in steps and journal_path.exists():
            steps["skills"] = safe_json(journal_path, default={})
        for honcho_snapshot in steps.get("honcho", []):
            matching = next(
                host for host in receipt.get("plan", {}).get("host_inventory", [])
                if host.get("host") == honcho_snapshot.get("host") and host.get("selected")
            )
            self.honcho.assert_unchanged(Path(matching["config_path"]), matching["host"], honcho_snapshot)
        # Fail closed before overwriting any file changed after apply.
        changed_files = []
        for section in ("skills", "memory_env", "invoke_agent"):
            changed_files.extend(steps.get(section, {}).get("changed_files", []))
        changed_files.extend(steps.get("host_allowlist", []))
        for item in changed_files:
            path = Path(item["path"])
            after = item.get("after")
            if after is None:
                if path.exists() or path.is_symlink():
                    raise StopCutover(f"concurrent drift at deleted path: {path}")
            elif not path.exists() or file_fingerprint(path) != after:
                raise StopCutover(f"concurrent drift at changed path: {path}")
        vault = steps.get("vault", {})
        source = Path(vault.get("source", "")) if vault else None
        destination = Path(vault.get("destination", "")) if vault else None
        if destination and destination.exists():
            if source.exists() or source.is_symlink():
                raise StopCutover("cannot restore vault: source path already exists")
            self.verify(receipt_id) if receipt.get("state") in {"applied", "verified"} else None
            os.rename(destination, source)
            fsync_path(source.parent)
            manifest_data = safe_json(source / "MANIFEST.json", default={})
            original_root = next(
                (
                    entry
                    for entry in manifest_data.get("before", {}).get("entries", [])
                    if entry.get("path") == "."
                ),
                None,
            )
            # Receipts live inside the archived vault and are not part of the
            # original payload. Remove only those exact generated files.
            for filename in VAULT_RECEIPT_FILES:
                (source / filename).unlink(missing_ok=True)
            if original_root:
                os.chmod(source, int(original_root["mode"]))
                try:
                    os.chown(source, int(original_root["uid"]), int(original_root["gid"]))
                except PermissionError:
                    pass
                os.utime(source, ns=(int(original_root["mtime_ns"]), int(original_root["mtime_ns"])))
            restored_manifest = tree_manifest(source)
            expected_hash = vault.get("tree_hash")
            if expected_hash and restored_manifest["tree_hash"] != expected_hash:
                raise StopCutover("restored vault does not match original tree hash")
        for item in reversed(changed_files):
            path = Path(item["path"])
            backup_text = item.get("backup")
            if not backup_text:
                continue
            backup = Path(backup_text)
            before = item["before"]
            if not backup.is_file():
                raise StopCutover(f"restore backup missing: {backup}")
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(path, backup.read_bytes(), int(before["mode"]))
            os.chmod(path, int(before["mode"]), follow_symlinks=False)
            try:
                os.chown(path, int(before["uid"]), int(before["gid"]), follow_symlinks=False)
            except PermissionError:
                pass
            os.utime(path, ns=(int(before["mtime_ns"]), int(before["mtime_ns"])), follow_symlinks=False)
        for moved in reversed(steps.get("skills", {}).get("moved", [])):
            source_path = Path(moved["source"])
            backup = Path(moved["backup"])
            if source_path.exists() or source_path.is_symlink():
                raise StopCutover(f"concurrent drift at removed skill path: {source_path}")
            if backup.exists():
                source_path.parent.mkdir(parents=True, exist_ok=True)
                os.rename(backup, source_path)
        for honcho_snapshot in reversed(steps.get("honcho", [])):
            matching = next(
                host for host in receipt.get("plan", {}).get("host_inventory", [])
                if host.get("host") == honcho_snapshot.get("host") and host.get("selected")
            )
            self.honcho.restore(Path(matching["config_path"]), matching["host"], honcho_snapshot)
        gmail_before = steps.get("gmail", {}).get("before", {})
        for unit, state in gmail_before.items():
            if state.get("enabled") == "enabled":
                self.runner.systemctl("enable", unit, check=False)
            if state.get("active") == "active":
                self.runner.systemctl("start", unit, check=False)
        if steps.get("gateway", {}).get("restarted"):
            self.runner.systemctl("restart", GATEWAY_UNIT, check=False)
        receipt["state"] = "restored"
        receipt["restored_at"] = datetime.now(timezone.utc).isoformat()
        receipt["automatic_restore"] = automatic
        atomic_write_json(receipt_dir / "RECEIPT.json", receipt)
        return {"receipt_id": receipt_id, "state": "restored"}


def discover_profile_homes(home: Path) -> list[Path]:
    profiles = home / "profiles"
    if not profiles.is_dir():
        return []
    return sorted(path for path in profiles.iterdir() if path.is_dir() and not path.is_symlink())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", nargs="?", choices=("preflight", "apply", "verify", "restore"), default="preflight")
    parser.add_argument("--home", type=Path, default=Path.home() / ".hermes")
    parser.add_argument("--systemd-dir", type=Path, default=Path.home() / ".config/systemd/user")
    parser.add_argument("--archive-root", type=Path, default=Path.home() / "archives/hermes-retired-note-system")
    parser.add_argument("--canonical-repo", type=Path, default=Path.home() / "hermes")
    parser.add_argument("--receipt-root", type=Path)
    parser.add_argument("--expected-merge-sha", default="")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--receipt-id", default="")
    parser.add_argument("--fault-after-step", default="", help=argparse.SUPPRESS)
    # Backward-compatible aliases from the earlier archive-only helper.
    parser.add_argument("--source", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--execute", action="store_true", help=argparse.SUPPRESS)
    return parser


def public_result(result: dict[str, Any]) -> dict[str, Any]:
    """Return a stdout-safe summary with no content or vault filenames."""
    if result.get("mode") != "preflight":
        return result
    vault = result.get("vault", {})
    manifest = vault.get("manifest", {})
    return {
        "mode": "preflight",
        "timestamp": result.get("timestamp"),
        "receipt_root": result.get("receipt_root"),
        "host_count": len(result.get("host_inventory", [])),
        "selected_host_count": sum(1 for host in result.get("host_inventory", []) if host.get("selected")),
        "honcho_scope_count": sum(len(scope.get("scopes", [])) for scope in result.get("honcho_scopes", [])),
        "skill_removal_count": len(result.get("skill_plan", [])),
        "vault": {
            "source": vault.get("source"),
            "destination": vault.get("destination"),
            "counts": manifest.get("counts", {}),
            "tree_hash": manifest.get("tree_hash", ""),
        },
        "gmail_unit_count": len(result.get("gmail_units", [])),
        "qmd_index": result.get("qmd_index"),
        "pid_docs_touched": False,
        "dry_run": True,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.execute:
        args.mode = "apply"
    home = args.home.expanduser().resolve()
    if args.source and args.source.expanduser().resolve() != home / "obsidian-vault":
        print(json.dumps({"state": "STOP", "reason": "--source must equal <home>/obsidian-vault"}, sort_keys=True))
        return 2
    receipt_root = (args.receipt_root or home / "retired-note-cutover-receipts").expanduser().resolve()
    paths = HostPaths(
        home=home,
        systemd_dir=args.systemd_dir.expanduser().resolve(),
        archive_root=args.archive_root.expanduser().resolve(),
        canonical_repo=args.canonical_repo.expanduser().resolve(),
        receipt_root=receipt_root,
        profile_homes=discover_profile_homes(home),
    )
    controller = CutoverController(paths, fault_after_step=args.fault_after_step)
    try:
        if args.mode == "preflight":
            result = controller.preflight(args.expected_merge_sha, require_merged=False)
        elif args.mode == "apply":
            result = controller.apply(args.expected_merge_sha, args.confirm)
        elif args.mode == "verify":
            if not args.receipt_id:
                raise StopCutover("verify requires --receipt-id")
            result = controller.verify(args.receipt_id)
        else:
            if not args.receipt_id:
                raise StopCutover("restore requires --receipt-id")
            result = controller.restore(args.receipt_id)
        print(json.dumps(public_result(result), indent=2, sort_keys=True, ensure_ascii=False))
        return 0
    except StopCutover as exc:
        print(json.dumps({"state": "STOP", "reason": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
    except Exception as exc:
        print(json.dumps({"state": "FAILED", "error_class": type(exc).__name__}, sort_keys=True), file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
