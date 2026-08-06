"""Private immutable store for versioned client-knowledge derived objects."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from hermes_constants import get_hermes_home
from hermes_cli.config import cfg_get, load_config

from .spool import _ensure_private_dir, _open_no_follow

DEFAULT_DERIVED_RELATIVE_PATH = "client-knowledge/derived"
_MAX_OBJECT_BYTES = 8 * 1024 * 1024


def canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("derived object is not canonical JSON") from exc


def versioned_identity(domain: str, *parts: str) -> str:
    payload = "\0".join((domain, *parts)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def resolve_derived_path(config: Mapping[str, object] | None = None) -> Path:
    cfg = dict(config or load_config() or {})
    raw = cfg_get(cfg, "client_knowledge", "extraction", "derived_path", default="")
    if not str(raw or "").strip():
        return get_hermes_home() / DEFAULT_DERIVED_RELATIVE_PATH
    path = Path(str(raw)).expanduser()
    if not path.is_absolute():
        raise ValueError("client_knowledge.extraction.derived_path must be absolute")
    return path


@dataclass(frozen=True, slots=True)
class DerivedRecord:
    object_id: str
    object_kind: str
    storage_id: str
    object_key: str
    sha256: str
    byte_size: int
    path: Path


class DerivedStore:
    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root).expanduser() if root is not None else resolve_derived_path()
        if not self.root.is_absolute():
            raise ValueError("derived store root must be absolute")
        _ensure_private_dir(self.root)
        identity = self.root / ".derived-id"
        if not identity.exists():
            temporary = self.root / f".derived-id.tmp-{secrets.token_hex(8)}"
            fd = _open_no_follow(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            try:
                with os.fdopen(fd, "wb") as handle:
                    fd = -1
                    handle.write(secrets.token_hex(32).encode("ascii"))
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.link(temporary, identity, follow_symlinks=False)
                except FileExistsError:
                    pass
            finally:
                if fd >= 0:
                    os.close(fd)
                if temporary.exists() and not temporary.is_symlink():
                    temporary.unlink()
        if identity.is_symlink() or identity.stat().st_mode & 0o077:
            raise ValueError("derived store identity is unsafe")
        self.storage_id = identity.read_text(encoding="ascii")
        if len(self.storage_id) != 64:
            raise ValueError("derived store identity is invalid")

    def path_for(self, kind: str, object_id: str) -> tuple[str, Path]:
        if kind not in {"extractions", "envelopes", "interpretations"}:
            raise ValueError("derived object kind is invalid")
        if len(object_id) != 64 or any(ch not in "0123456789abcdef" for ch in object_id):
            raise ValueError("derived object identity is invalid")
        key = f"{kind}/{object_id[:2]}/{object_id}/object.json"
        path = self.root / kind / object_id[:2] / object_id / "object.json"
        if path.is_symlink():
            raise ValueError("derived object path is unsafe")
        return key, path

    def put_json(self, kind: str, object_id: str, value: Any) -> DerivedRecord:
        data = canonical_json(value)
        if len(data) > _MAX_OBJECT_BYTES:
            raise ValueError("derived object exceeds its byte limit")
        key, path = self.path_for(kind, object_id)
        _ensure_private_dir(path.parent)
        digest = hashlib.sha256(data).hexdigest()
        temporary = path.parent / f".object.tmp-{secrets.token_hex(16)}"
        fd = _open_no_follow(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        try:
            with os.fdopen(fd, "wb") as handle:
                fd = -1
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, path, follow_symlinks=False)
            except FileExistsError:
                existing = self.read_json(kind, object_id, digest, len(data))
                if canonical_json(existing) != data:
                    raise ValueError("derived object identity conflicts with immutable bytes")
            directory_fd = _open_no_follow(
                path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if fd >= 0:
                os.close(fd)
            if temporary.exists() and not temporary.is_symlink():
                temporary.unlink()
        return DerivedRecord(object_id, kind, self.storage_id, key, digest, len(data), path)

    def read_json(
        self, kind: str, object_id: str, expected_sha256: str = "", expected_size: int = -1
    ) -> Any:
        _key, path = self.path_for(kind, object_id)
        if path.is_symlink() or not path.is_file():
            raise FileNotFoundError(object_id)
        fd = _open_no_follow(path, os.O_RDONLY)
        try:
            with os.fdopen(fd, "rb") as handle:
                fd = -1
                data = handle.read(_MAX_OBJECT_BYTES + 1)
        finally:
            if fd >= 0:
                os.close(fd)
        if len(data) > _MAX_OBJECT_BYTES:
            raise ValueError("derived object exceeds its byte limit")
        if expected_size >= 0 and len(data) != expected_size:
            raise ValueError("derived object size conflicts with its receipt")
        if expected_sha256 and hashlib.sha256(data).hexdigest() != expected_sha256:
            raise ValueError("derived object hash conflicts with its receipt")
        try:
            return json.loads(data)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("derived object is invalid JSON") from exc


__all__ = [
    "DEFAULT_DERIVED_RELATIVE_PATH",
    "DerivedRecord",
    "DerivedStore",
    "canonical_json",
    "resolve_derived_path",
    "versioned_identity",
]
