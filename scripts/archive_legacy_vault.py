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
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable

MARKER = "obsidian"
CONFIRMATION = "RETIRE-OBSIDIAN-POST-MERGE"
RECEIPT_VERSION = 2
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
GATEWAY_DRAIN_TIMEOUT_SECONDS = 120.0
GATEWAY_DRAIN_POLL_SECONDS = 0.25
GATEWAY_RESTART_TIMEOUT_SECONDS = 30.0
GATEWAY_OWNED_WORKER_RE = re.compile(
    r"(?:^|[/\s])(?:run_agent\.py|hermes(?:_cli)?\s+kanban|kanban_codex_worker|delegate_tool\.py)(?:\s|$)"
)
ARCHIVE_RECEIPT_FILES = (
    "MANIFEST.json",
    "SHA256SUMS",
    "FILE_METADATA.tsv",
    "VERIFICATION_RECEIPT.json",
)
JOURNAL_FILE = "TRANSACTION.jsonl"
RESTORE_JOURNAL_FILE = "RESTORE.jsonl"


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


class TransactionJournal:
    """Append-only, fsynced, hash-chained write-ahead transaction journal."""

    def __init__(self, path: Path, *, fault: Callable[[str], None] | None = None) -> None:
        self.path = path
        self.fault = fault or (lambda _label: None)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            atomic_write_bytes(self.path, b"", RECEIPT_FILE_MODE)
        os.chmod(self.path, RECEIPT_FILE_MODE)
        self.records = self.read_records(self.path)
        self.head = self.records[-1]["hash"] if self.records else "0" * 64
        self.next_seq = len(self.records) + 1

    @staticmethod
    def read_records(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        records: list[dict[str, Any]] = []
        previous = "0" * 64
        for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            try:
                record = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise StopCutover(f"malformed transaction journal at line {line_number}") from exc
            if not isinstance(record, dict):
                raise StopCutover(f"malformed transaction journal at line {line_number}")
            claimed = str(record.get("hash", ""))
            unsigned = {key: value for key, value in record.items() if key != "hash"}
            if record.get("previous_hash") != previous:
                raise StopCutover(f"transaction journal chain break at line {line_number}")
            calculated = sha256_bytes(
                json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
            )
            if claimed != calculated:
                raise StopCutover(f"transaction journal hash mismatch at line {line_number}")
            if record.get("seq") != line_number or record.get("phase") not in {"intent", "complete", "seal"}:
                raise StopCutover(f"malformed transaction journal at line {line_number}")
            if not isinstance(record.get("action"), str) or not isinstance(record.get("data"), dict):
                raise StopCutover(f"malformed transaction journal at line {line_number}")
            if not isinstance(record.get("op_id"), str) or not record["op_id"]:
                raise StopCutover(f"malformed transaction journal at line {line_number}")
            records.append(record)
            previous = claimed
        return records

    def append(self, phase: str, action: str, data: dict[str, Any], *, op_id: str = "") -> dict[str, Any]:
        record: dict[str, Any] = {
            "seq": self.next_seq,
            "phase": phase,
            "action": action,
            "op_id": op_id or f"op-{self.next_seq:06d}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": data,
            "previous_hash": self.head,
        }
        record["hash"] = sha256_bytes(
            json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        )
        payload = (json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
        with self.path.open("ab", buffering=0) as handle:
            handle.write(payload)
            os.fsync(handle.fileno())
        fsync_path(self.path.parent)
        self.records.append(record)
        self.head = record["hash"]
        self.next_seq += 1
        self.fault(f"journal-{action}-{phase}")
        return record

    def intent(self, action: str, data: dict[str, Any]) -> dict[str, Any]:
        return self.append("intent", action, data)

    def complete(self, intent: dict[str, Any], data: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.append("complete", intent["action"], data or {}, op_id=intent["op_id"])

    def after_mutation(self, action: str) -> None:
        self.fault(f"{action}-mutation")

    def seal(self, data: dict[str, Any]) -> dict[str, Any]:
        return self.append("seal", "transaction_seal", data)

    def completed_ids(self) -> set[str]:
        return {record["op_id"] for record in self.records if record.get("phase") == "complete"}

    def intents(self) -> list[dict[str, Any]]:
        return [record for record in self.records if record.get("phase") == "intent"]


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


def _validate_archive_rel_path(raw: str) -> str:
    if not isinstance(raw, str) or not raw or raw == ".":
        raise ValueError("invalid archive relative path")
    if any(character in raw for character in ("\t", "\r", "\n")):
        raise ValueError("archive relative path contains an unsupported control character")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("unsafe archive relative path")
    if path.as_posix() != raw:
        raise ValueError("non-canonical archive relative path")
    return path.as_posix()


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

    def scrub(self, config_path: Path, host: str, journal: TransactionJournal) -> dict[str, Any]:
        from plugins.memory.honcho.client import HonchoClientConfig, get_honcho_client, reset_honcho_client

        config = HonchoClientConfig.from_global_config(host=host, config_path=config_path)
        if not config.enabled:
            return {"host": host, "changed_cards": [], "corrective_conclusions": [], "remaining_search_hits": 0}
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
        corrections: list[dict[str, Any]] = []
        context_scopes: list[dict[str, Any]] = []
        for observer_id in peers:
            observer = client.peer(observer_id)
            for target_id in peers:
                card = list(observer.get_card(target=target_id) or [])
                clean_card = [fact for fact in card if MARKER not in str(fact).lower()]
                if clean_card != card:
                    intent = journal.intent("honcho_card_set", {
                        "config_path": str(config_path), "host": host,
                        "observer": observer_id, "target": target_id,
                        "before": card, "after": clean_card,
                    })
                    observer.set_card(clean_card, target=target_id)
                    journal.after_mutation("honcho_card_set")
                    journal.complete(intent, {"applied": True})
                    changed_cards.append({
                        "observer": observer_id,
                        "target": target_id,
                        "before": card,
                        "after": clean_card,
                    })
                scope = observer.conclusions_of(target_id)
                matched_ids: list[str] = []
                for conclusion in self._all_conclusions(scope):
                    if MARKER in str(getattr(conclusion, "content", "")).lower():
                        matched_ids.append(str(getattr(conclusion, "id", "")))
                if matched_ids:
                    correction_token = uuid.uuid4().hex
                    correction_text = (
                        "The previously recorded Obsidian-based note-system guidance is retired and "
                        "must not be used as a current capability or source of truth. "
                        f"[cutover-correction:{correction_token}]"
                    )
                    intent = journal.intent("honcho_correction_create", {
                        "config_path": str(config_path), "host": host,
                        "observer": observer_id, "target": target_id,
                        "matched_conclusion_ids": matched_ids,
                        "content": correction_text,
                        "correction_token": correction_token,
                        "limitations": (
                            "Original conclusions are preserved because the SDK cannot restore exact "
                            "server IDs, timestamps, or reasoning metadata."
                        ),
                    })
                    created = scope.create([{"content": correction_text, "session_id": None}])
                    created_items = list(getattr(created, "items", created) or [])
                    created_ids = [str(getattr(item, "id", "")) for item in created_items if getattr(item, "id", None)]
                    journal.after_mutation("honcho_correction_create")
                    journal.complete(intent, {"created_ids": created_ids})
                    corrections.append({
                        "observer": observer_id, "target": target_id,
                        "content": correction_text, "created_ids": created_ids,
                        "matched_conclusion_ids": matched_ids,
                    })
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
            "corrective_conclusions": corrections,
            "deleted_conclusions": [],
            "context_scopes": context_scopes,
            "remaining_search_hits": remaining,
            "messages_preserved": True,
            "sessions_preserved": True,
        }

    def restore(
        self,
        config_path: Path,
        host: str,
        snapshot: dict[str, Any],
        journal: TransactionJournal | None = None,
    ) -> None:
        from plugins.memory.honcho.client import HonchoClientConfig, get_honcho_client, reset_honcho_client

        config = HonchoClientConfig.from_global_config(host=host, config_path=config_path)
        if not config.enabled:
            return
        reset_honcho_client()
        client = get_honcho_client(config)
        for changed in snapshot.get("changed_cards", []):
            observer = client.peer(changed["observer"])
            current = list(observer.get_card(target=changed["target"]) or [])
            if current == changed["before"]:
                continue
            if current != changed["after"]:
                raise StopCutover("concurrent Honcho card drift; refusing restore")
            intent = journal.intent("restore_honcho_card", {
                "config_path": str(config_path), "host": host,
                "observer": changed["observer"], "target": changed["target"],
                "before": current, "after": changed["before"],
            }) if journal else None
            observer.set_card(changed["before"], target=changed["target"])
            if journal and intent:
                journal.complete(intent, {"applied": True})
        for correction in snapshot.get("corrective_conclusions", []):
            scope = client.peer(correction["observer"]).conclusions_of(correction["target"])
            created_ids = [value for value in correction.get("created_ids", []) if value]
            existing = self._all_conclusions(scope)
            existing_by_id = {
                str(getattr(item, "id", "")): item for item in existing if getattr(item, "id", None)
            }
            if created_ids:
                for conclusion_id in created_ids:
                    if conclusion_id not in existing_by_id:
                        continue
                    intent = journal.intent("restore_honcho_correction_delete", {
                        "config_path": str(config_path), "host": host,
                        "observer": correction["observer"], "target": correction["target"],
                        "id": conclusion_id,
                    }) if journal else None
                    scope.delete(conclusion_id)
                    if journal and intent:
                        journal.complete(intent, {"deleted": True})
            else:
                exact_matches = [
                    conclusion_id for conclusion_id, item in existing_by_id.items()
                    if str(getattr(item, "content", "")) == correction.get("content")
                ]
                if exact_matches:
                    for conclusion_id in exact_matches:
                        intent = journal.intent("restore_honcho_correction_delete", {
                            "config_path": str(config_path), "host": host,
                            "observer": correction["observer"], "target": correction["target"],
                            "id": conclusion_id,
                        }) if journal else None
                        scope.delete(conclusion_id)
                        if journal and intent:
                            journal.complete(intent, {"deleted": True})
                    continue
                if not correction.get("completed"):
                    continue
                text = "Rollback correction: the prior retirement correction was reverted; review current policy before relying on historical note-system conclusions."
                intent = journal.intent("restore_honcho_compensation_create", {
                    "config_path": str(config_path), "host": host,
                    "observer": correction["observer"], "target": correction["target"],
                    "content": text,
                }) if journal else None
                scope.create([{
                    "content": text,
                    "session_id": None,
                }])
                if journal and intent:
                    journal.complete(intent, {"created": True})

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
            if current not in (changed["before"], changed["after"]):
                raise StopCutover("concurrent Honcho card drift; refusing restore")
        # Original conclusions are intentionally untouched. Cutover-created
        # correction IDs are owned by this receipt and may be compensated.


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
        self._journal: TransactionJournal | None = None

    def _fault(self, step: str) -> None:
        if self.fault_after_step == step:
            raise RuntimeError(f"injected crash after {step}")

    def _require_journal(self) -> TransactionJournal:
        if self._journal is None:
            raise StopCutover("transaction journal is not initialized")
        return self._journal

    def _journaled_file_rewrite(
        self,
        path: Path,
        data: bytes,
        *,
        receipt_dir: Path,
        kind: str,
    ) -> dict[str, Any]:
        before_exists = path.exists() or path.is_symlink()
        before = file_fingerprint(path) if before_exists else None
        backup = receipt_dir / "files-backup" / sha256_bytes(str(path).encode())[:20]
        intent = self._require_journal().intent("file_rewrite", {
            "path": str(path), "backup": str(backup) if before_exists else "",
            "before": before, "kind": kind, "after_sha256": sha256_bytes(data),
        })
        if before_exists:
            backup.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(backup, path.read_bytes(), RECEIPT_FILE_MODE)
        if before_exists:
            atomic_rewrite_preserving_metadata(path, data)
        else:
            atomic_write_bytes(path, data, RECEIPT_FILE_MODE)
        after = file_fingerprint(path)
        self._fault(f"{kind}-mutation")
        self._require_journal().complete(intent, {"after": after})
        self._fault(kind)
        return {"path": str(path), "backup": str(backup) if before_exists else "", "before": before, "after": after, "kind": kind}

    def _journaled_delete(self, path: Path, *, receipt_dir: Path, kind: str) -> dict[str, Any]:
        if not path.exists() and not path.is_symlink():
            return {"path": str(path), "before": None, "after": None, "kind": kind}
        before = file_fingerprint(path)
        backup = receipt_dir / "files-backup" / sha256_bytes(str(path).encode())[:20]
        intent = self._require_journal().intent("file_delete", {
            "path": str(path), "backup": str(backup), "before": before, "kind": kind,
        })
        backup.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(backup, path.read_bytes(), RECEIPT_FILE_MODE)
        path.unlink()
        fsync_path(path.parent)
        self._fault(f"{kind}-mutation")
        self._require_journal().complete(intent, {"absent": True})
        self._fault(kind)
        return {"path": str(path), "backup": str(backup), "before": before, "after": None, "kind": kind}

    def _journaled_rename(self, source: Path, destination: Path, *, kind: str) -> dict[str, Any]:
        before = tree_manifest(source) if source.is_dir() and not source.is_symlink() else file_fingerprint(source)
        intent = self._require_journal().intent("rename", {
            "source": str(source), "destination": str(destination), "before": before, "kind": kind,
        })
        os.rename(source, destination)
        fsync_path(source.parent)
        fsync_path(destination.parent)
        after = tree_manifest(destination) if destination.is_dir() and not destination.is_symlink() else file_fingerprint(destination)
        self._fault(f"{kind}-mutation")
        self._require_journal().complete(intent, {"after": after})
        self._fault(kind)
        return {"source": str(source), "backup": str(destination), "before": before, "after": after, "kind": kind}

    def _journaled_systemctl(self, args: list[str], unit: str, *, before: dict[str, str]) -> str:
        intent = self._require_journal().intent("systemctl", {
            "args": args, "unit": unit, "before": before,
        })
        output = self.runner.systemctl(*args, check=True)
        after = {
            "active": self.runner.systemctl("is-active", unit, check=False),
            "enabled": self.runner.systemctl("is-enabled", unit, check=False),
        }
        self._fault(f"systemctl-{unit}-{args[0]}-mutation")
        self._require_journal().complete(intent, {"after": after})
        self._fault(f"systemctl-{unit}-{args[0]}")
        return output

    def _journaled_command(self, action: str, args: list[str], *, rollback: dict[str, Any]) -> str:
        intent = self._require_journal().intent(action, {"args": args, "rollback": rollback})
        result = self.runner.run(args, timeout=900, check=True)
        self._fault(f"{action}-mutation")
        self._require_journal().complete(intent, {"returncode": result.returncode})
        self._fault(action)
        return result.stdout.strip()

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
            default_host = str(raw.get("defaultHost", "")).strip()
            if default_host and not hosts:
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
        # Membership is operator-owned. Discovery must never shadow a legacy
        # dot alias, re-enable a disabled alias, or make an unknown profile
        # selectable by adding an empty block.
        del receipt_dir
        return []

    def _repo_gate(self, expected_merge_sha: str) -> dict[str, Any]:
        if not re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", expected_merge_sha):
            raise StopCutover("expected merged removal SHA must be a full immutable commit hash")
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

    def _gateway_pid_record(self) -> dict[str, Any]:
        pid_path = self.paths.home / "gateway.pid"
        if pid_path.is_symlink() or not pid_path.is_file():
            raise StopCutover("gateway PID file is missing or unsafe")
        try:
            raw = pid_path.read_text(encoding="utf-8").strip()
            parsed = json.loads(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StopCutover("gateway PID file is malformed") from exc
        if isinstance(parsed, bool):
            raise StopCutover("gateway PID file is malformed")
        record = parsed if isinstance(parsed, dict) else {"pid": parsed}
        try:
            pid = int(record.get("pid"))
        except (TypeError, ValueError) as exc:
            raise StopCutover("gateway PID file is malformed") from exc
        if pid <= 0:
            raise StopCutover("gateway PID file is malformed")
        return {**record, "pid": pid}

    def _gateway_runtime_state(self, *, allow_draining: bool = False) -> dict[str, Any]:
        state_path = self.paths.home / "gateway_state.json"
        if state_path.is_symlink() or not state_path.is_file():
            raise StopCutover("gateway runtime status is missing or unsafe")
        state = safe_json(state_path)
        if not isinstance(state, dict):
            raise StopCutover("gateway runtime status must be an object")
        if "active_agents" not in state:
            raise StopCutover("gateway active_agents status is malformed")
        raw = state["active_agents"]
        if isinstance(raw, bool):
            raise StopCutover("gateway active_agents status is malformed")
        try:
            active_agents = int(raw)
        except (TypeError, ValueError) as exc:
            raise StopCutover("gateway active_agents status is malformed") from exc
        if active_agents < 0:
            raise StopCutover("gateway active_agents status is malformed")
        gateway_state = state.get("gateway_state")
        allowed_states = {"running", "draining"} if allow_draining else {"running"}
        if gateway_state not in allowed_states:
            raise StopCutover(f"gateway runtime state is not drainable: {gateway_state!r}")
        pid_record = self._gateway_pid_record()
        try:
            state_pid = int(state.get("pid"))
        except (TypeError, ValueError) as exc:
            raise StopCutover("gateway runtime PID identity is malformed") from exc
        if state_pid != pid_record["pid"]:
            raise StopCutover("gateway runtime status does not match the PID file")
        state_start = state.get("start_time")
        pid_start = pid_record.get("start_time")
        if state_start is not None and pid_start is not None and state_start != pid_start:
            raise StopCutover("gateway runtime status has stale process identity")
        return {
            "active_agents": active_agents,
            "gateway_state": gateway_state,
            "pid": state_pid,
            "start_time": state_start,
        }

    def _gateway_owned_workers(self) -> list[dict[str, Any]]:
        gateway_pid = self._gateway_pid_record()["pid"]
        result = self.runner.run(
            ["ps", "--no-headers", "--ppid", str(gateway_pid), "-o", "pid=,args="],
            timeout=30,
            check=False,
        )
        if result.returncode not in {0, 1}:
            raise StopCutover(f"gateway-owned process inspection failed: exit {result.returncode}")
        workers: list[dict[str, Any]] = []
        for line in result.stdout.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) != 2 or not parts[0].isdigit():
                continue
            if GATEWAY_OWNED_WORKER_RE.search(parts[1]):
                workers.append({"pid": int(parts[0]), "kind": "gateway-owned-worker"})
        return workers

    def _gateway_idle_gate(self) -> dict[str, Any]:
        if self.runner.systemctl("is-active", GATEWAY_UNIT, check=False) != "active":
            raise StopCutover("gateway service is not active")
        state = self._gateway_runtime_state()
        workers = self._gateway_owned_workers()
        if state["active_agents"] or workers:
            raise StopCutover(
                f"gateway work is active (active_agents={state['active_agents']}, owned_workers={len(workers)})"
            )
        return {**state, "owned_workers": []}

    def _drain_and_restart_gateway(self, source: Path) -> dict[str, Any]:
        from gateway.drain_control import current_instantiation_epoch

        marker = self.paths.home / ".drain_request.json"
        marker_before = None
        marker_backup = ""
        if marker.exists() or marker.is_symlink():
            marker_before = file_fingerprint(marker)
            backup = self._require_journal().path.parent / "files-backup" / "drain-marker-before"
            marker_backup = str(backup)
        payload = {
            "action": "drain",
            "requested_at": datetime.now(timezone.utc).isoformat(),
            "principal": "retired-knowledge-cutover",
            "epoch": current_instantiation_epoch(),
            "suppress_notification": False,
        }
        intent = self._require_journal().intent("gateway_drain_marker_write", {
            "path": str(marker), "before": marker_before, "backup": marker_backup,
            "payload": payload,
        })
        if marker_before:
            backup = Path(marker_backup)
            backup.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(backup, marker.read_bytes(), RECEIPT_FILE_MODE)
        atomic_write_json(marker, payload)
        self._fault("gateway-drain-marker-mutation")
        self._require_journal().complete(intent, {"after": file_fingerprint(marker)})
        self._fault("gateway-drain-marker")

        deadline = time.monotonic() + GATEWAY_DRAIN_TIMEOUT_SECONDS
        samples = 0
        drained_state: dict[str, Any] | None = None
        while True:
            state = self._gateway_runtime_state(allow_draining=True)
            workers = self._gateway_owned_workers()
            samples += 1
            if state["active_agents"] == 0 and not workers:
                drained_state = state
                break
            if time.monotonic() >= deadline:
                raise StopCutover("gateway drain timed out before owned work became idle")
            time.sleep(GATEWAY_DRAIN_POLL_SECONDS)

        before = {
            "active": self.runner.systemctl("is-active", GATEWAY_UNIT, check=False),
            "enabled": self.runner.systemctl("is-enabled", GATEWAY_UNIT, check=False),
        }
        self._journaled_systemctl(["restart", GATEWAY_UNIT], GATEWAY_UNIT, before=before)
        if self.runner.systemctl("is-active", GATEWAY_UNIT, check=False) != "active":
            raise StopCutover("gateway did not become active after restart")
        restart_deadline = time.monotonic() + GATEWAY_RESTART_TIMEOUT_SECONDS
        restarted_state: dict[str, Any] | None = None
        while True:
            try:
                candidate = self._gateway_runtime_state(allow_draining=True)
            except StopCutover:
                candidate = None
            if candidate and drained_state and (
                candidate["pid"] != drained_state["pid"]
                or candidate.get("start_time") != drained_state.get("start_time")
            ):
                restarted_state = candidate
                break
            if time.monotonic() >= restart_deadline:
                raise StopCutover("restarted gateway process identity did not become observable")
            time.sleep(GATEWAY_DRAIN_POLL_SECONDS)
        if source.exists() or source.is_symlink():
            raise StopCutover("gateway runtime recreated the retired vault path after restart")
        clear_intent = self._require_journal().intent("gateway_drain_marker_clear", {
            "path": str(marker), "before": file_fingerprint(marker),
        })
        marker.unlink(missing_ok=True)
        fsync_path(marker.parent)
        self._fault("gateway-drain-marker-clear-mutation")
        self._require_journal().complete(clear_intent, {"absent": True})
        running_deadline = time.monotonic() + GATEWAY_RESTART_TIMEOUT_SECONDS
        while True:
            try:
                running_state = self._gateway_runtime_state()
            except StopCutover:
                running_state = None
            if running_state and restarted_state and (
                running_state["pid"] == restarted_state["pid"]
                and running_state.get("start_time") == restarted_state.get("start_time")
            ):
                break
            if time.monotonic() >= running_deadline:
                raise StopCutover("restarted gateway did not return to running after drain clear")
            time.sleep(GATEWAY_DRAIN_POLL_SECONDS)
        if source.exists() or source.is_symlink():
            raise StopCutover("gateway runtime recreated the retired vault path after returning to running")
        return {
            "restarted": True,
            "active": True,
            "drain_samples": samples,
            "source_absent": True,
            "before_identity": {
                "pid": drained_state["pid"], "start_time": drained_state.get("start_time"),
            },
            "after_identity": {
                "pid": running_state["pid"], "start_time": running_state.get("start_time"),
            },
        }

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
        run_dir = destination.parent
        if run_dir == source or source.is_relative_to(run_dir) or run_dir.is_relative_to(source):
            raise StopCutover("archive layout collides with the vault source")
        reserved = {"vault", *ARCHIVE_RECEIPT_FILES, JOURNAL_FILE, RESTORE_JOURNAL_FILE}
        if len(reserved) != 7 or destination.name != "vault":
            raise StopCutover("archive reserved-path layout is ambiguous")
        manifest = tree_manifest(source)
        for entry in manifest["entries"]:
            if entry["path"] == ".":
                continue
            try:
                _validate_archive_rel_path(entry["path"])
            except ValueError as exc:
                raise StopCutover(f"vault contains an unsupported archive path: {entry['path']!r}") from exc
        return {
            "source": str(source),
            "destination": str(destination),
            "run_dir": str(run_dir),
            "device": source_device,
            "source_identity": file_fingerprint(source),
            "manifest": manifest,
        }

    def _journaled_artifact_write(self, path: Path, data: bytes, *, action: str) -> dict[str, Any]:
        intent = self._require_journal().intent(action, {
            "path": str(path), "before": None, "sha256": sha256_bytes(data), "size": len(data),
        })
        atomic_write_bytes(path, data, RECEIPT_FILE_MODE)
        after = file_fingerprint(path)
        self._fault(f"{action}-mutation")
        self._require_journal().complete(intent, {"after": after})
        self._fault(action)
        return {"path": str(path), "sha256": after["sha256"], "size": after["size"]}

    def _write_vault_receipts(self, run_dir: Path, vault: Path, payload: dict[str, Any]) -> dict[str, Any]:
        manifest = {"version": RECEIPT_VERSION, "payload": payload}
        manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
        sums = []
        metadata = ["path\tkind\tsize\tmode\tuid\tgid\tmtime_ns\tsha256"]
        for entry in payload["entries"]:
            if entry["kind"] == "file":
                sums.append(f"{entry['sha256']}  {entry['path']}")
            metadata.append(
                "\t".join(str(entry[key]) for key in ("path", "kind", "size", "mode", "uid", "gid", "mtime_ns", "sha256"))
            )
        sums_bytes = ("\n".join(sums) + "\n").encode()
        metadata_bytes = ("\n".join(metadata) + "\n").encode()
        self._journaled_artifact_write(run_dir / "MANIFEST.json", manifest_bytes, action="archive_manifest_write")
        self._journaled_artifact_write(run_dir / "SHA256SUMS", sums_bytes, action="archive_sums_write")
        self._journaled_artifact_write(run_dir / "FILE_METADATA.tsv", metadata_bytes, action="archive_metadata_write")
        fsync_tree(vault)
        outer = {
            "version": RECEIPT_VERSION,
            "receipt_id": self._require_journal().path.parent.name,
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "payload_manifest_sha256": sha256_bytes(manifest_bytes),
            "payload_tree_hash": payload["tree_hash"],
            "sha256sums_sha256": sha256_bytes(sums_bytes),
            "metadata_tsv_sha256": sha256_bytes(metadata_bytes),
            "archive_path": str(vault),
            "archive_device": vault.stat().st_dev,
            "source_path": payload["root"],
            "source_identity": payload["entries"][0],
            "counts": payload["counts"],
        }
        intent = self._require_journal().intent("archive_outer_receipt_write", {
            "path": str(run_dir / "VERIFICATION_RECEIPT.json"), "outer": outer,
        })
        outer["journal_anchor"] = intent["hash"]
        outer_bytes = (json.dumps(outer, indent=2, sort_keys=True) + "\n").encode()
        atomic_write_bytes(run_dir / "VERIFICATION_RECEIPT.json", outer_bytes, RECEIPT_FILE_MODE)
        self._fault("archive_outer_receipt_write-mutation")
        self._require_journal().complete(intent, {"after": file_fingerprint(run_dir / "VERIFICATION_RECEIPT.json")})
        self._fault("archive_outer_receipt_write")
        fsync_tree(vault)
        for artifact in ARCHIVE_RECEIPT_FILES:
            fsync_path(run_dir / artifact)
        for directory in (run_dir, run_dir.parent, run_dir.parent.parent):
            if directory.is_dir():
                fsync_path(directory)
        return outer

    def _archive_vault(self, vault_plan: dict[str, Any]) -> dict[str, Any]:
        source = Path(vault_plan["source"])
        destination = Path(vault_plan["destination"])
        self._vault_open_handle_gate(source)
        run_dir = Path(vault_plan["run_dir"])
        run_dir.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(run_dir.parent, 0o700)
        if source.stat().st_dev != run_dir.parent.stat().st_dev:
            raise StopCutover("device changed after preflight")
        run_dir.mkdir(mode=0o700)
        fsync_path(run_dir.parent)
        before = tree_manifest(source)
        if before["tree_hash"] != vault_plan["manifest"]["tree_hash"]:
            raise StopCutover("vault changed after preflight")
        self._journaled_rename(source, destination, kind="vault-rename")
        try:
            after = tree_manifest(destination)
            if before["tree_hash"] != after["tree_hash"]:
                raise StopCutover("vault changed during atomic rename")
            archive_payload = {**after, "root": str(source)}
            outer = self._write_vault_receipts(run_dir, destination, archive_payload)
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
            "run_dir": str(run_dir),
            "outer_receipt": outer,
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
        for item in planned:
            root = Path(item["root"])
            source = Path(item["path"])
            if not source.exists():
                raise StopCutover(f"planned skill path drifted: {source}")
            relative = source.relative_to(root)
            backup = backup_root / sha256_bytes(str(root).encode())[:12] / relative
            backup.parent.mkdir(parents=True, exist_ok=True)
            move = self._journaled_rename(source, backup, kind="skill-rename")
            move.update({"root": str(root), "name": item["name"]})
            moved.append(move)
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
                    changed_files.append(self._journaled_file_rewrite(
                        path, after_bytes, receipt_dir=receipt_dir, kind="skill-config-edit"
                    ))
            snapshot = root.parent / ".skills_prompt_snapshot.json"
            if snapshot.exists():
                changed_files.append(self._journaled_delete(
                    snapshot, receipt_dir=receipt_dir, kind="skill-snapshot-delete"
                ))
            for cache in (root / ".hub/index-cache", root / "index-cache"):
                if cache.exists():
                    backup = backup_root / sha256_bytes(str(root).encode())[:12] / cache.relative_to(root)
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    move = self._journaled_rename(cache, backup, kind="skill-cache-rename")
                    move.update({"root": str(root), "name": "index-cache"})
                    moved.append(move)
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
                    changed.append(self._journaled_file_rewrite(
                        path, after_bytes, receipt_dir=receipt_dir, kind="memory-edit"
                    ))
            for env_path in (home / ".env", home / "gmail-intake.env"):
                if not env_path.exists():
                    continue
                before_bytes = env_path.read_bytes()
                after_text, removed_names = scrub_env_text(before_bytes.decode("utf-8", errors="strict"))
                if removed_names:
                    item = self._journaled_file_rewrite(
                        env_path, after_text.encode(), receipt_dir=receipt_dir, kind="environment-edit"
                    )
                    item["removed_names"] = removed_names
                    changed.append(item)
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
        return {"changed_files": [self._journaled_file_rewrite(
            script, changed_text.encode(), receipt_dir=receipt_dir, kind="invoke-agent-edit"
        )]}

    def _stop_legacy_gmail(self) -> dict[str, Any]:
        before = {unit: {
            "active": self.runner.systemctl("is-active", unit, check=False),
            "enabled": self.runner.systemctl("is-enabled", unit, check=False),
        } for unit in GMAIL_UNITS}
        self._journaled_systemctl(
            ["disable", "--now", "gmail-intake-pubsub.service"],
            "gmail-intake-pubsub.service", before=before["gmail-intake-pubsub.service"],
        )
        self._journaled_systemctl(
            ["disable", "--now", "gmail-intake-watch-renew.timer"],
            "gmail-intake-watch-renew.timer", before=before["gmail-intake-watch-renew.timer"],
        )
        self._journaled_systemctl(
            ["stop", "gmail-intake-watch-renew.service"],
            "gmail-intake-watch-renew.service", before=before["gmail-intake-watch-renew.service"],
        )
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
        qmd_before = {
            "active": self.runner.systemctl("is-active", QMD_SKILLS_REFRESH_UNIT, check=False),
            "enabled": self.runner.systemctl("is-enabled", QMD_SKILLS_REFRESH_UNIT, check=False),
        }
        service_result = self._journaled_systemctl(
            ["start", QMD_SKILLS_REFRESH_UNIT], QMD_SKILLS_REFRESH_UNIT, before=qmd_before
        )
        status = self.runner.systemctl("is-failed", QMD_SKILLS_REFRESH_UNIT, check=False)
        fallback = False
        if status == "failed":
            fallback = True
            generator = Path.home() / ".local/bin/qmd-skills-catalog"
            if generator.exists():
                self._journaled_command("qmd_generator", [str(generator)], rollback={"kind": "refresh-only"})
            self._journaled_command("qmd_update", ["qmd", "--index", "skills", "update"], rollback={"kind": "refresh-only"})
            self._journaled_command(
                "qmd_embed",
                ["qmd", "--index", "skills", "embed", "--max-docs-per-batch", "50", "--max-batch-mb", "2"],
                rollback={"kind": "refresh-only"},
            )
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
        self._journal = TransactionJournal(receipt_dir / JOURNAL_FILE, fault=self._fault)
        self._journaled_artifact_write(
            receipt_dir / "PRECHECK.json",
            (json.dumps(plan, indent=2, sort_keys=True) + "\n").encode(),
            action="precheck_write",
        )
        receipt: dict[str, Any] = {
            "version": RECEIPT_VERSION,
            "receipt_id": receipt_id,
            "state": "applying",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "expected_merge_sha": expected_merge_sha,
            "plan": plan,
            "steps": {},
        }
        self._journaled_artifact_write(
            receipt_dir / "RECEIPT.json",
            (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode(),
            action="receipt_initial_write",
        )
        try:
            receipt["steps"]["host_allowlist"] = self._ensure_unknown_hosts_closed(receipt_dir)
            receipt["steps"]["honcho"] = []
            for host in plan["host_inventory"]:
                if not host["selected"]:
                    continue
                receipt["steps"]["honcho"].append(
                    self.honcho.scrub(Path(host["config_path"]), host["host"], self._require_journal())
                )
            receipt["steps"]["gmail"] = self._stop_legacy_gmail()
            receipt["steps"]["invoke_agent"] = self._neutralize_invoke_agent(receipt_dir)
            receipt["steps"]["skills"] = self._apply_skill_reconcile(receipt_dir, plan["skill_plan"])
            receipt["steps"]["memory_env"] = self._apply_memory_env(receipt_dir)
            self._fault("host-files")
            receipt["steps"]["qmd"] = self._qmd_refresh()
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
            receipt["steps"]["vault"] = self._archive_vault(plan["vault"])
            self._fault("vault-archived")
            receipt["steps"]["gateway"] = self._drain_and_restart_gateway(
                Path(plan["vault"]["source"])
            )
            receipt["state"] = "applied"
            receipt["completed_at"] = datetime.now(timezone.utc).isoformat()
            self._journaled_artifact_write(
                receipt_dir / "RECEIPT.json",
                (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode(),
                action="receipt_applied_write",
            )
            verification = self.verify(receipt_id, seal=False)
            receipt["state"] = "verified"
            receipt["verification"] = verification
            self._journaled_artifact_write(
                receipt_dir / "RECEIPT.json",
                (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode(),
                action="receipt_verified_write",
            )
            self._require_journal().seal({
                "receipt_id": receipt_id,
                "state": "verified",
                "vault_tree_hash": verification["vault_tree_hash"],
            })
            return {"receipt_id": receipt_id, "receipt_dir": str(receipt_dir), "state": "verified"}
        except BaseException as exc:
            receipt["state"] = "failed"
            receipt["failure_class"] = type(exc).__name__
            try:
                self._journaled_artifact_write(
                    receipt_dir / "RECEIPT.json",
                    (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode(),
                    action="receipt_failed_write",
                )
            except BaseException:
                pass
            try:
                self.restore(receipt_id, automatic=True)
            except BaseException as restore_exc:
                receipt["rollback_failure_class"] = type(restore_exc).__name__
                try:
                    atomic_write_json(receipt_dir / "ROLLBACK_FAILURE.json", receipt)
                except BaseException:
                    pass
            raise

    def _load_receipt(self, receipt_id: str) -> tuple[Path, dict[str, Any]]:
        receipt_dir = self._receipt_dir(receipt_id)
        receipt = safe_json(receipt_dir / "RECEIPT.json")
        if not isinstance(receipt, dict):
            raise StopCutover("receipt is malformed")
        return receipt_dir, receipt

    @staticmethod
    def _parse_metadata_tsv(path: Path) -> list[dict[str, Any]]:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            raise StopCutover("archive metadata table is unreadable") from exc
        expected_header = "path\tkind\tsize\tmode\tuid\tgid\tmtime_ns\tsha256"
        if not lines or lines[0] != expected_header:
            raise StopCutover("archive metadata table header is malformed")
        rows: list[dict[str, Any]] = []
        for line_number, line in enumerate(lines[1:], 2):
            parts = line.split("\t")
            if len(parts) != 8:
                raise StopCutover(f"archive metadata table is malformed at line {line_number}")
            raw_path, kind, size, mode, uid, gid, mtime_ns, digest = parts
            try:
                safe_path = "." if raw_path == "." else _validate_archive_rel_path(raw_path)
                rows.append({
                    "path": safe_path, "kind": kind, "size": int(size), "mode": int(mode),
                    "uid": int(uid), "gid": int(gid), "mtime_ns": int(mtime_ns), "sha256": digest,
                })
            except (ValueError, TypeError) as exc:
                raise StopCutover(f"archive metadata table is malformed at line {line_number}") from exc
        return rows

    @staticmethod
    def _parse_sha256sums(path: Path) -> dict[str, str]:
        result: dict[str, str] = {}
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            raise StopCutover("archive SHA256SUMS is unreadable") from exc
        for line_number, line in enumerate(lines, 1):
            if not line:
                continue
            if "  " not in line:
                raise StopCutover(f"archive SHA256SUMS is malformed at line {line_number}")
            digest, raw_path = line.split("  ", 1)
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise StopCutover(f"archive SHA256SUMS is malformed at line {line_number}")
            safe_path = _validate_archive_rel_path(raw_path)
            if safe_path in result:
                raise StopCutover("archive SHA256SUMS contains a duplicate path")
            result[safe_path] = digest
        return result

    def _verify_archive_artifacts(self, run_dir: Path, destination: Path) -> dict[str, Any]:
        if run_dir.is_symlink() or not run_dir.is_dir():
            raise StopCutover("archive run directory is missing or unsafe")
        if destination != run_dir / "vault":
            raise StopCutover("archive payload path does not match reserved layout")
        for filename in ARCHIVE_RECEIPT_FILES:
            path = run_dir / filename
            if path.is_symlink() or not path.is_file():
                raise StopCutover(f"archive receipt missing or unsafe: {filename}")
        outer = safe_json(run_dir / "VERIFICATION_RECEIPT.json")
        manifest_path = run_dir / "MANIFEST.json"
        sums_path = run_dir / "SHA256SUMS"
        metadata_path = run_dir / "FILE_METADATA.tsv"
        if sha256_file(manifest_path) != outer.get("payload_manifest_sha256"):
            raise StopCutover("archive manifest hash does not match outer receipt")
        if sha256_file(sums_path) != outer.get("sha256sums_sha256"):
            raise StopCutover("archive SHA256SUMS hash does not match outer receipt")
        if sha256_file(metadata_path) != outer.get("metadata_tsv_sha256"):
            raise StopCutover("archive metadata hash does not match outer receipt")
        if str(destination) != outer.get("archive_path") or destination.stat().st_dev != outer.get("archive_device"):
            raise StopCutover("archive path/device does not match outer receipt")
        journal_path = self._receipt_dir(str(outer.get("receipt_id", ""))) / JOURNAL_FILE if outer.get("receipt_id") else None
        # The archive run can be verified from a copied receipt root; bind the
        # actual transaction journal supplied by the active receipt directory.
        if journal_path is None or not journal_path.exists():
            journal_path = self._require_journal().path if self._journal else None
        if journal_path is None or not journal_path.exists():
            raise StopCutover("transaction journal is missing")
        journal_records = TransactionJournal.read_records(journal_path)
        anchor = outer.get("journal_anchor")
        anchors = [record for record in journal_records if record.get("hash") == anchor]
        if len(anchors) != 1:
            raise StopCutover("outer receipt journal anchor is missing from transaction chain")
        anchor_record = anchors[0]
        if (
            anchor_record.get("phase") != "intent"
            or anchor_record.get("action") != "archive_outer_receipt_write"
            or anchor_record.get("data", {}).get("path") != str(run_dir / "VERIFICATION_RECEIPT.json")
        ):
            raise StopCutover("outer receipt journal anchor does not identify its write intent")
        expected_outer = anchor_record.get("data", {}).get("outer")
        if (
            not isinstance(expected_outer, dict)
            or set(outer) != {*expected_outer, "journal_anchor"}
            or any(outer.get(key) != value for key, value in expected_outer.items())
        ):
            raise StopCutover("outer receipt fields do not match the journaled write intent")
        manifest = safe_json(manifest_path)
        payload = manifest.get("payload") if isinstance(manifest, dict) else None
        if not isinstance(payload, dict) or not isinstance(payload.get("entries"), list):
            raise StopCutover("archive manifest is malformed")
        if payload.get("tree_hash") != outer.get("payload_tree_hash"):
            raise StopCutover("archive tree hash does not match outer receipt")
        metadata_rows = self._parse_metadata_tsv(metadata_path)
        if metadata_rows != payload["entries"]:
            raise StopCutover("archive metadata table does not match manifest")
        sums = self._parse_sha256sums(sums_path)
        expected_sums = {
            entry["path"]: entry["sha256"] for entry in payload["entries"]
            if entry["kind"] == "file"
        }
        if sums != expected_sums:
            raise StopCutover("archive SHA256SUMS does not match manifest")
        actual = tree_manifest(destination)
        if actual["tree_hash"] != payload.get("tree_hash") or actual["entries"] != payload["entries"]:
            raise StopCutover("archive payload does not match authenticated manifest")
        if payload.get("entries", [{}])[0] != outer.get("source_identity"):
            raise StopCutover("archive source identity does not match outer receipt")
        return {"outer": outer, "payload": payload, "journal_head": journal_records[-1]["hash"] if journal_records else "0" * 64}

    @staticmethod
    def _completion_for(journal: TransactionJournal, op_id: str) -> dict[str, Any] | None:
        return next(
            (record for record in journal.records if record.get("phase") == "complete" and record.get("op_id") == op_id),
            None,
        )

    @staticmethod
    def _fingerprint_matches(actual: dict[str, Any], expected: dict[str, Any]) -> bool:
        return all(value is None or actual.get(key) == value for key, value in expected.items())

    @staticmethod
    def _tree_matches(actual: dict[str, Any], expected: dict[str, Any]) -> bool:
        return (
            actual.get("tree_hash") == expected.get("tree_hash")
            and actual.get("entries") == expected.get("entries")
            and actual.get("counts") == expected.get("counts")
        )

    @staticmethod
    def _honcho_snapshots_from_journal(journal: TransactionJournal) -> list[dict[str, Any]]:
        completed = journal.completed_ids()
        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        for intent in journal.intents():
            action = intent["action"]
            if action not in {"honcho_card_set", "honcho_correction_create"}:
                continue
            data = intent["data"]
            key = (str(data.get("config_path", "")), str(data.get("host", "")))
            snapshot = grouped.setdefault(key, {
                "config_path": key[0], "host": key[1],
                "changed_cards": [], "corrective_conclusions": [],
            })
            completion = CutoverController._completion_for(journal, intent["op_id"])
            if action == "honcho_card_set":
                snapshot["changed_cards"].append({
                    "observer": data["observer"], "target": data["target"],
                    "before": data["before"], "after": data["after"],
                    "completed": intent["op_id"] in completed,
                })
            else:
                snapshot["corrective_conclusions"].append({
                    "observer": data["observer"], "target": data["target"],
                    "content": data["content"],
                    "created_ids": list((completion or {}).get("data", {}).get("created_ids", [])),
                    "completed": intent["op_id"] in completed,
                })
        return list(grouped.values())

    def _require_transaction_seal(self, journal: TransactionJournal, receipt_id: str) -> dict[str, Any]:
        if not journal.records or journal.records[-1].get("phase") != "seal":
            raise StopCutover("transaction journal is not sealed at its final record")
        seal = journal.records[-1]
        if (
            seal.get("action") != "transaction_seal"
            or seal.get("data", {}).get("receipt_id") != receipt_id
            or seal.get("data", {}).get("state") != "verified"
        ):
            raise StopCutover("transaction journal final seal is malformed")
        return seal

    def verify(self, receipt_id: str, *, seal: bool = True) -> dict[str, Any]:
        receipt_dir, receipt = self._load_receipt(receipt_id)
        self._journal = TransactionJournal(receipt_dir / JOURNAL_FILE, fault=self._fault)
        if receipt.get("state") not in {"applied", "verified"}:
            raise StopCutover(f"receipt state is not verifiable: {receipt.get('state')}")
        vault = receipt.get("steps", {}).get("vault", {})
        source = Path(vault.get("source", ""))
        destination = Path(vault.get("destination", ""))
        if source.exists() or source.is_symlink():
            raise StopCutover("vault source was recreated")
        if not destination.is_dir() or destination.is_symlink():
            raise StopCutover("archive destination is missing or unsafe")
        run_dir = Path(vault.get("run_dir", destination.parent))
        authenticated = self._verify_archive_artifacts(run_dir, destination)
        payload = authenticated["payload"]
        gmail = {
            unit: self.runner.systemctl("is-active", unit, check=False)
            for unit in GMAIL_UNITS
        }
        if any(value in {"active", "activating"} for value in gmail.values()):
            raise StopCutover("legacy Gmail collector remains active")
        if self.runner.systemctl("is-active", GATEWAY_UNIT, check=False) != "active":
            raise StopCutover("gateway is not active")
        if not receipt.get("steps", {}).get("gateway", {}).get("source_absent"):
            raise StopCutover("gateway runtime recreation check was not recorded")
        if receipt.get("state") == "verified":
            self._require_transaction_seal(self._require_journal(), receipt_id)
        verification = {
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "receipt_dir": str(receipt_dir),
            "vault_tree_hash": payload["tree_hash"],
            "gmail_units": gmail,
            "gateway_active": True,
            "source_absent": True,
            "pid_docs_touched": False,
            "journal_head": authenticated["journal_head"],
        }
        self._journaled_artifact_write(
            receipt_dir / "VERIFY.json",
            (json.dumps(verification, indent=2, sort_keys=True) + "\n").encode(),
            action="verify_receipt_write",
        )
        if seal:
            self._require_journal().seal({
                "receipt_id": receipt_id,
                "state": "verified",
                "vault_tree_hash": payload["tree_hash"],
            })
        return verification

    def restore(self, receipt_id: str, automatic: bool = False) -> dict[str, Any]:
        receipt_dir, receipt = self._load_receipt(receipt_id)
        if receipt.get("state") == "restored":
            return {"receipt_id": receipt_id, "state": "restored", "idempotent": True}
        apply_journal = TransactionJournal(receipt_dir / JOURNAL_FILE, fault=self._fault)
        if not automatic and receipt.get("state") in {"applied", "verified"}:
            self._require_transaction_seal(apply_journal, receipt_id)
        restore_journal = TransactionJournal(receipt_dir / RESTORE_JOURNAL_FILE, fault=self._fault)
        self._journal = restore_journal
        completed = apply_journal.completed_ids()
        intents = apply_journal.intents()
        mutation_states: dict[str, str] = {}
        honcho_snapshots = self._honcho_snapshots_from_journal(apply_journal)
        drain_clear_intents = [intent for intent in intents if intent["action"] == "gateway_drain_marker_clear"]
        qmd_refresh_intents = [
            intent for intent in intents
            if intent["action"] in {"qmd_generator", "qmd_update", "qmd_embed"}
        ]
        archive_artifact_actions = {
            "archive_manifest_write", "archive_sums_write", "archive_metadata_write",
            "archive_outer_receipt_write",
        }

        # Classify every owned local mutation before any rollback write. An
        # incomplete intent may mean either "not started" or "mutated before
        # completion was journaled"; only exact pre/post states are accepted.
        for intent in intents:
            data = intent["data"]
            action = intent["action"]
            if action in {"file_rewrite", "file_delete"}:
                path = Path(data["path"])
                before = data.get("before")
                exists = path.exists() or path.is_symlink()
                actual = file_fingerprint(path) if exists else None
                if isinstance(before, dict) and actual == before:
                    mutation_states[intent["op_id"]] = "pre"
                    continue
                completion = self._completion_for(apply_journal, intent["op_id"])
                after = (completion or {}).get("data", {}).get("after")
                if action == "file_delete" and not exists:
                    mutation_states[intent["op_id"]] = "post"
                    continue
                if isinstance(after, dict) and actual == after:
                    mutation_states[intent["op_id"]] = "post"
                    continue
                if action == "file_rewrite" and isinstance(actual, dict):
                    expected = {
                        "kind": "file", "sha256": data.get("after_sha256"),
                        "mode": before.get("mode") if isinstance(before, dict) else RECEIPT_FILE_MODE,
                        "uid": before.get("uid") if isinstance(before, dict) else None,
                        "gid": before.get("gid") if isinstance(before, dict) else None,
                    }
                    if self._fingerprint_matches(actual, expected):
                        mutation_states[intent["op_id"]] = "post"
                        continue
                if before is None and not exists:
                    mutation_states[intent["op_id"]] = "pre"
                    continue
                raise StopCutover(f"concurrent drift at changed path: {path}")
            elif action == "rename":
                source = Path(data["source"])
                destination = Path(data["destination"])
                source_exists = source.exists() or source.is_symlink()
                destination_exists = destination.exists() or destination.is_symlink()
                if source_exists and not destination_exists:
                    current = tree_manifest(source) if source.is_dir() and not source.is_symlink() else file_fingerprint(source)
                    before = data.get("before")
                    matches = (
                        self._tree_matches(current, before)
                        if isinstance(current, dict) and "tree_hash" in current and isinstance(before, dict)
                        else current == before
                    )
                    if not matches:
                        raise StopCutover(f"concurrent drift at renamed path: {source}")
                    mutation_states[intent["op_id"]] = "pre"
                elif destination_exists and not source_exists:
                    current = tree_manifest(destination) if destination.is_dir() and not destination.is_symlink() else file_fingerprint(destination)
                    completion = self._completion_for(apply_journal, intent["op_id"])
                    expected = (completion or {}).get("data", {}).get("after", data.get("before"))
                    matches = (
                        self._tree_matches(current, expected)
                        if isinstance(current, dict) and "tree_hash" in current and isinstance(expected, dict)
                        else current == expected
                    )
                    if not matches:
                        raise StopCutover(f"concurrent drift at renamed path: {source}")
                    mutation_states[intent["op_id"]] = "post"
                else:
                    raise StopCutover(f"concurrent drift at renamed path: {source}")
            elif action == "gateway_drain_marker_write":
                marker = Path(data["path"])
                before = data.get("before")
                exists = marker.exists() or marker.is_symlink()
                actual = file_fingerprint(marker) if exists else None
                if actual == before:
                    mutation_states[intent["op_id"]] = "pre"
                else:
                    completion = self._completion_for(apply_journal, intent["op_id"])
                    after = (completion or {}).get("data", {}).get("after")
                    payload_hash = sha256_bytes(
                        (json.dumps(data.get("payload", {}), indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()
                    )
                    cleared_after_write = any(
                        clear["data"].get("path") == str(marker)
                        and not exists
                        for clear in drain_clear_intents
                    )
                    if cleared_after_write or (
                        isinstance(actual, dict)
                        and (
                            actual == after
                            or (actual.get("kind") == "file" and actual.get("sha256") == payload_hash)
                        )
                    ):
                        mutation_states[intent["op_id"]] = "post"
                    else:
                        raise StopCutover(f"concurrent drift at drain marker: {marker}")
            elif action == "systemctl":
                unit = data["unit"]
                current = {
                    "active": self.runner.systemctl("is-active", unit, check=False),
                    "enabled": self.runner.systemctl("is-enabled", unit, check=False),
                }
                before = data.get("before", {})
                completion = self._completion_for(apply_journal, intent["op_id"])
                after = (completion or {}).get("data", {}).get("after")
                if current == before:
                    mutation_states[intent["op_id"]] = "pre"
                elif isinstance(after, dict) and current == after:
                    mutation_states[intent["op_id"]] = "post"
                else:
                    args = data.get("args", [])
                    operation = args[0] if args else ""
                    expected = dict(before)
                    if operation == "disable":
                        expected["enabled"] = "disabled"
                        if "--now" in args:
                            expected["active"] = "inactive"
                    elif operation == "stop":
                        expected["active"] = "inactive"
                    elif operation in {"start", "restart"}:
                        expected["active"] = "active"
                    elif operation == "enable":
                        expected["enabled"] = "enabled"
                    if current == expected:
                        mutation_states[intent["op_id"]] = "post"
                    else:
                        raise StopCutover(f"concurrent drift at systemd unit: {unit}")
            elif action in archive_artifact_actions:
                path = Path(data["path"])
                if not path.exists() and not path.is_symlink():
                    mutation_states[intent["op_id"]] = "pre"
                    continue
                if path.is_symlink() or not path.is_file():
                    raise StopCutover(f"concurrent drift at archive artifact: {path}")
                if action == "archive_outer_receipt_write":
                    expected = dict(data.get("outer", {}))
                    expected["journal_anchor"] = intent["hash"]
                    expected_bytes = (json.dumps(expected, indent=2, sort_keys=True) + "\n").encode()
                    expected_sha = sha256_bytes(expected_bytes)
                else:
                    expected_sha = data.get("sha256")
                if sha256_file(path) != expected_sha:
                    raise StopCutover(f"concurrent drift at archive artifact: {path}")
                mutation_states[intent["op_id"]] = "post"
            elif action in {
                "precheck_write", "receipt_initial_write", "receipt_applied_write",
                "receipt_verified_write", "receipt_failed_write", "verify_receipt_write",
            }:
                # Durable receipt artifacts are transaction evidence, not host
                # state. Restore deliberately preserves them.
                continue

        # Remote drift must be rejected before local rollback begins.
        for snapshot in honcho_snapshots:
            self.honcho.assert_unchanged(Path(snapshot["config_path"]), snapshot["host"], snapshot)

        # Authenticate the archive before moving it back. Artifacts remain in
        # run_dir; no payload filename is ever deleted or interpreted as control.
        vault_intent = next((i for i in reversed(intents) if i["action"] == "rename" and i["data"].get("kind") == "vault-rename"), None)
        restored_vault_hash = ""
        if vault_intent and mutation_states.get(vault_intent["op_id"]) == "post":
            source = Path(vault_intent["data"]["source"])
            destination = Path(vault_intent["data"]["destination"])
            if destination.exists():
                if source.exists() or source.is_symlink():
                    raise StopCutover("cannot restore vault: source path already exists")
                outer_path = destination.parent / "VERIFICATION_RECEIPT.json"
                if outer_path.is_file():
                    authenticated = self._verify_archive_artifacts(destination.parent, destination)
                    expected_payload = authenticated["payload"]
                else:
                    expected_payload = vault_intent["data"].get("before")
                    if not automatic or not isinstance(expected_payload, dict):
                        raise StopCutover("authenticated archive receipt is missing")
                    actual_incomplete = tree_manifest(destination)
                    if (
                        actual_incomplete.get("tree_hash") != expected_payload.get("tree_hash")
                        or actual_incomplete.get("entries") != expected_payload.get("entries")
                    ):
                        raise StopCutover("incomplete archive payload does not match journaled pre-state")
                restore_intent = restore_journal.intent("restore_vault_rename", {
                    "source": str(destination), "destination": str(source),
                    "expected_tree_hash": expected_payload["tree_hash"],
                })
                os.rename(destination, source)
                fsync_path(destination.parent)
                fsync_path(source.parent)
                restored = tree_manifest(source)
                if restored["tree_hash"] != expected_payload["tree_hash"]:
                    raise StopCutover("restored vault does not match authenticated payload")
                restore_journal.complete(restore_intent, {"restored_tree_hash": restored["tree_hash"]})
                restored_vault_hash = restored["tree_hash"]

        # Reverse every non-vault local mutation from intent pre-state, even if
        # the apply mutation threw before a completion record could be written.
        for intent in reversed(intents):
            data = intent["data"]
            action = intent["action"]
            if action == "rename" and data.get("kind") != "vault-rename":
                source = Path(data["source"])
                destination = Path(data["destination"])
                if mutation_states.get(intent["op_id"]) == "post":
                    rollback = restore_journal.intent("restore_rename", {"source": str(destination), "destination": str(source)})
                    source.parent.mkdir(parents=True, exist_ok=True)
                    os.rename(destination, source)
                    fsync_path(source.parent)
                    restore_journal.complete(rollback, {"restored": True})
            elif action in {"file_rewrite", "file_delete"} and mutation_states.get(intent["op_id"]) == "post":
                path = Path(data["path"])
                before = data["before"]
                rollback = restore_journal.intent("restore_file", {"path": str(path), "before": before})
                if before is None:
                    path.unlink(missing_ok=True)
                    fsync_path(path.parent)
                    restore_journal.complete(rollback, {"absent": True})
                else:
                    backup = Path(data["backup"])
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
                    restored = file_fingerprint(path)
                    if restored != before:
                        raise StopCutover(f"restored file metadata does not match pre-state: {path}")
                    restore_journal.complete(rollback, {"after": restored})
            elif action == "systemctl" and mutation_states.get(intent["op_id"]) == "post":
                unit = data["unit"]
                state = data["before"]
                operation = data.get("args", [""])[0]
                if operation == "disable" and state.get("enabled") == "enabled":
                    rollback = restore_journal.intent("restore_systemctl_enable", {"unit": unit})
                    self.runner.systemctl("enable", unit, check=True)
                    restore_journal.complete(rollback, {"enabled": state.get("enabled")})
                active_action = "start" if state.get("active") == "active" else "stop"
                rollback = restore_journal.intent(f"restore_systemctl_{active_action}", {"unit": unit})
                self.runner.systemctl(active_action, unit, check=True)
                restore_journal.complete(rollback, {"active": state.get("active")})
            elif action == "gateway_drain_marker_write" and mutation_states.get(intent["op_id"]) == "post":
                marker = Path(data["path"])
                rollback = restore_journal.intent("restore_drain_marker", {"path": str(marker), "before": data.get("before")})
                if data.get("before") and data.get("backup"):
                    atomic_write_bytes(marker, Path(data["backup"]).read_bytes(), int(data["before"]["mode"]))
                else:
                    marker.unlink(missing_ok=True)
                    fsync_path(marker.parent)
                restore_journal.complete(rollback, {"restored": True})
            elif action in archive_artifact_actions and mutation_states.get(intent["op_id"]) == "post":
                path = Path(data["path"])
                rollback = restore_journal.intent("restore_archive_artifact_delete", {"path": str(path)})
                path.unlink()
                fsync_path(path.parent)
                restore_journal.complete(rollback, {"absent": True})

        # QMD refreshes are derived state rather than byte-restorable state.
        # Re-run the exact targeted refresh sequence only after skill/config
        # rollback has restored the pre-cutover source corpus.
        for intent in qmd_refresh_intents:
            args = list(intent["data"].get("args", []))
            if not args:
                raise StopCutover("QMD rollback command is missing")
            rollback = restore_journal.intent("restore_qmd_refresh", {"args": args})
            result = self.runner.run(args, timeout=900, check=True)
            restore_journal.complete(rollback, {"returncode": result.returncode})

        # Remote rollback is reconstructed from write-ahead intents, not the
        # late aggregate receipt, so post-mutation/pre-completion crashes remain
        # recoverable.
        for snapshot in reversed(honcho_snapshots):
            self.honcho.restore(Path(snapshot["config_path"]), snapshot["host"], snapshot, restore_journal)

        receipt["state"] = "restored"
        receipt["restored_at"] = datetime.now(timezone.utc).isoformat()
        receipt["automatic_restore"] = automatic
        receipt["restore_journal_head"] = restore_journal.head
        atomic_write_json(receipt_dir / "RECEIPT.json", receipt)
        return {"receipt_id": receipt_id, "state": "restored", "vault_tree_hash": restored_vault_hash}


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
