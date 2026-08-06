"""Private, crash-safe raw artifact spool for client-knowledge intake."""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterable, Mapping

from hermes_constants import get_hermes_home
from hermes_cli.config import cfg_get, load_config

from .models import storage_key
from .models import IntakeArtifact
from .scope import ClientKnowledgeValidationError


DEFAULT_SPOOL_RELATIVE_PATH = "client-knowledge/raw"
_DIR_MODE = 0o700
_FILE_MODE = 0o600
_CHUNK_SIZE = 1024 * 1024
_STORAGE_ID_FILE = ".spool-id"


class RawSpoolRootMismatch(ClientKnowledgeValidationError):
    """Raised when a database receipt belongs to another spool identity."""


@dataclass(frozen=True, slots=True)
class SpoolRecord:
    """Opaque result of writing one raw artifact."""

    storage_key: str
    storage_id: str
    sha256: str
    byte_size: int
    path: Path


def resolve_spool_path(config: Mapping[str, object] | None = None) -> Path:
    """Resolve a profile-scoped, operator-configurable spool directory."""
    cfg = dict(config or load_config() or {})
    raw = cfg_get(cfg, "client_knowledge", "intake", "spool_path", default="")
    if raw is None or not str(raw).strip():
        path = get_hermes_home() / DEFAULT_SPOOL_RELATIVE_PATH
    else:
        path = Path(str(raw)).expanduser()
        if not path.is_absolute():
            raise ClientKnowledgeValidationError(
                "client_knowledge.intake.spool_path must be absolute"
            )
    return path


def _reject_symlink_components(path: Path) -> None:
    """Reject symlinks in a spool path before creating or opening it."""
    current = Path(path.anchor) if path.anchor else Path()
    for part in path.parts[1:] if path.is_absolute() else path.parts:
        current = current / part
        try:
            if current.is_symlink():
                raise ClientKnowledgeValidationError(
                    "client knowledge spool path may not contain symlinks"
                )
        except OSError as exc:
            raise ClientKnowledgeValidationError("cannot inspect client knowledge spool path") from exc


def _ensure_private_dir(path: Path) -> None:
    _reject_symlink_components(path)
    current = Path(path.anchor) if path.anchor else Path()
    parts = path.parts[1:] if path.is_absolute() else path.parts
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise ClientKnowledgeValidationError(
                "client knowledge spool path may not contain symlinks"
            )
        if current.exists():
            if not current.is_dir():
                raise ClientKnowledgeValidationError(
                    "client knowledge spool path is not a directory"
                )
            continue
        current.mkdir(mode=_DIR_MODE)
        current.chmod(_DIR_MODE)
    _reject_symlink_components(path)
    if path.stat().st_mode & 0o077:
        raise ClientKnowledgeValidationError(
            "client knowledge spool directory must not be group/world accessible"
        )
    if not stat.S_ISDIR(path.stat().st_mode):
        raise ClientKnowledgeValidationError("client knowledge spool path is not a directory")


def _open_no_follow(path: Path, flags: int, mode: int = _FILE_MODE) -> int:
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(path, flags, mode)
    except OSError as exc:
        raise ClientKnowledgeValidationError("client knowledge spool file is unsafe") from exc


def _source_chunks(source: BinaryIO | Iterable[bytes]) -> Iterable[bytes]:
    if hasattr(source, "read"):
        while True:
            chunk = source.read(_CHUNK_SIZE)
            if not chunk:
                break
            if not isinstance(chunk, (bytes, bytearray, memoryview)):
                raise ClientKnowledgeValidationError("raw artifact stream must yield bytes")
            yield bytes(chunk)
        return
    for chunk in source:
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise ClientKnowledgeValidationError("raw artifact stream must yield bytes")
        if chunk:
            yield bytes(chunk)


class RawSpool:
    """Store raw bytes under opaque keys without leaking source names."""

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root).expanduser() if root is not None else resolve_spool_path()
        if not self.root.is_absolute():
            raise ClientKnowledgeValidationError("raw spool root must be absolute")
        _ensure_private_dir(self.root)
        self.storage_id = self._load_or_create_storage_id()

    def _load_or_create_storage_id(self) -> str:
        """Return the persistent identity copied with this spool on restore."""
        path = self.root / _STORAGE_ID_FILE
        if path.is_symlink():
            raise ClientKnowledgeValidationError("raw spool identity may not be a symlink")
        value = secrets.token_hex(32)
        temp_path = self.root / f".{_STORAGE_ID_FILE}.tmp-{secrets.token_hex(16)}"
        fd = -1
        try:
            if not path.exists():
                fd = _open_no_follow(
                    temp_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    _FILE_MODE,
                )
                with os.fdopen(fd, "wb", closefd=True) as handle:
                    fd = -1
                    handle.write(value.encode("ascii"))
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.link(temp_path, path, follow_symlinks=False)
                except FileExistsError:
                    pass
                root_fd = _open_no_follow(
                    self.root,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                )
                try:
                    os.fsync(root_fd)
                finally:
                    os.close(root_fd)
            read_fd = _open_no_follow(path, os.O_RDONLY)
            try:
                raw = os.read(read_fd, 128).decode("ascii")
            finally:
                os.close(read_fd)
        except (OSError, UnicodeError) as exc:
            raise ClientKnowledgeValidationError("raw spool identity is unreadable") from exc
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                if temp_path.exists() and not temp_path.is_symlink():
                    temp_path.unlink()
            except OSError:
                pass
        if len(raw) != 64 or any(ch not in "0123456789abcdef" for ch in raw):
            raise ClientKnowledgeValidationError("raw spool identity is invalid")
        if path.stat().st_mode & 0o077:
            raise ClientKnowledgeValidationError("raw spool identity must be private")
        return raw

    def path_for_key(self, key: str) -> Path:
        if not isinstance(key, str) or not key or any(ch not in "0123456789abcdef" for ch in key):
            raise ClientKnowledgeValidationError("storage key is not safe")
        if len(key) < 32 or len(key) > 128:
            raise ClientKnowledgeValidationError("storage key is not safe")
        artifact_dir = self.root / key
        path = artifact_dir / "raw"
        if artifact_dir.parent != self.root or artifact_dir.name != key:
            raise ClientKnowledgeValidationError("storage key escapes raw spool")
        if artifact_dir.is_symlink() or path.is_symlink():
            raise ClientKnowledgeValidationError("storage key escapes raw spool")
        return path

    def put(
        self,
        *,
        provider_id: str,
        provider_artifact_id: str,
        project_key: str = "",
        source: BinaryIO | Iterable[bytes],
        expected_sha256: str = "",
        expected_size: int | None = None,
    ) -> SpoolRecord:
        """Stream *source* to an fsynced same-directory temp and replace."""
        key = storage_key(provider_id, provider_artifact_id, project_key)
        destination = self.path_for_key(key)
        _ensure_private_dir(self.root)
        artifact_dir = destination.parent
        artifact_dir_existed = artifact_dir.exists()
        _ensure_private_dir(artifact_dir)
        if destination.is_symlink():
            raise ClientKnowledgeValidationError("raw spool destination may not be a symlink")

        temp_name = f".{key}.tmp-{secrets.token_hex(16)}"
        temp_path = artifact_dir / temp_name
        if temp_path.parent != artifact_dir:
            raise ClientKnowledgeValidationError("raw spool temporary path escaped root")
        digest = hashlib.sha256()
        size = 0
        fd = -1
        try:
            fd = _open_no_follow(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _FILE_MODE)
            os.fchmod(fd, _FILE_MODE)
            with os.fdopen(fd, "wb", closefd=True) as handle:
                fd = -1
                for chunk in _source_chunks(source):
                    digest.update(chunk)
                    size += len(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_path, _FILE_MODE)
            if (
                (expected_sha256 and digest.hexdigest() != expected_sha256)
                or (expected_size is not None and size != expected_size)
            ):
                raise ClientKnowledgeValidationError(
                    "raw spool receipt does not match artifact metadata"
                )
            try:
                os.link(temp_path, destination, follow_symlinks=False)
            except FileExistsError:
                if destination.is_symlink():
                    raise ClientKnowledgeValidationError(
                        "raw spool destination may not be a symlink"
                    )
                existing_digest, existing_size = self._digest_file(destination)
                if existing_digest != digest.hexdigest() or existing_size != size:
                    raise ClientKnowledgeValidationError(
                        "provider identity was reused with different raw bytes"
                    )
                return SpoolRecord(
                    key,
                    self.storage_id,
                    existing_digest,
                    existing_size,
                    destination,
                )
            # fsync the containing directory so a committed DB row cannot point
            # at a filename lost across a power failure.
            dir_fd = _open_no_follow(artifact_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
            if not artifact_dir_existed:
                root_fd = _open_no_follow(
                    self.root,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                )
                try:
                    os.fsync(root_fd)
                finally:
                    os.close(root_fd)
            return SpoolRecord(
                key,
                self.storage_id,
                digest.hexdigest(),
                size,
                destination,
            )
        except OSError as exc:
            raise ClientKnowledgeValidationError("raw artifact spool write failed") from exc
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                if temp_path.exists() and not temp_path.is_symlink():
                    temp_path.unlink()
            except OSError:
                pass

    write = put

    def preserve_artifact(
        self,
        artifact: IntakeArtifact,
        source: BinaryIO | Iterable[bytes],
    ) -> SpoolRecord:
        """Preserve bytes and prove they match the immutable artifact metadata."""
        return self.put(
            provider_id=artifact.provider_id,
            provider_artifact_id=artifact.provider_artifact_id,
            project_key=artifact.project_key,
            source=source,
            expected_sha256=artifact.content_sha256,
            expected_size=artifact.byte_size,
        )

    def read(self, key: str) -> BinaryIO:
        """Open an opaque spool object without following symlinks."""
        path = self.path_for_key(key)
        if not path.is_file() or path.is_symlink():
            raise FileNotFoundError(key)
        fd = _open_no_follow(path, os.O_RDONLY)
        return os.fdopen(fd, "rb", closefd=True)

    def verify(
        self,
        key: str,
        *,
        storage_id: str,
        expected_sha256: str,
        expected_size: int,
    ) -> SpoolRecord:
        """Prove that this root contains the expected immutable raw object."""
        if storage_id and storage_id != self.storage_id:
            raise RawSpoolRootMismatch("raw spool root does not match its receipt")
        path = self.path_for_key(key)
        digest, size = self._digest_file(path)
        if digest != expected_sha256 or size != expected_size:
            raise ClientKnowledgeValidationError("raw spool object failed receipt verification")
        return SpoolRecord(key, self.storage_id, digest, size, path)

    def read_verified(
        self,
        key: str,
        *,
        storage_id: str,
        expected_sha256: str,
        expected_size: int,
    ) -> BinaryIO:
        """Verify an immutable receipt immediately before opening its bytes."""
        if storage_id and storage_id != self.storage_id:
            raise RawSpoolRootMismatch("raw spool root does not match its receipt")
        path = self.path_for_key(key)
        fd = _open_no_follow(path, os.O_RDONLY)
        handle = os.fdopen(fd, "rb", closefd=True)
        digest = hashlib.sha256()
        size = 0
        try:
            while True:
                chunk = handle.read(_CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
            if digest.hexdigest() != expected_sha256 or size != expected_size:
                raise ClientKnowledgeValidationError(
                    "raw spool object failed receipt verification"
                )
            handle.seek(0)
            return handle
        except BaseException:
            handle.close()
            raise

    @staticmethod
    def _digest_file(path: Path) -> tuple[str, int]:
        if path.is_symlink() or not path.is_file():
            raise ClientKnowledgeValidationError("raw spool object is not a regular file")
        digest = hashlib.sha256()
        size = 0
        fd = _open_no_follow(path, os.O_RDONLY)
        try:
            with os.fdopen(fd, "rb", closefd=True) as handle:
                fd = -1
                while True:
                    chunk = handle.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    digest.update(chunk)
                    size += len(chunk)
        finally:
            if fd >= 0:
                os.close(fd)
        return digest.hexdigest(), size


__all__ = [
    "DEFAULT_SPOOL_RELATIVE_PATH",
    "RawSpool",
    "RawSpoolRootMismatch",
    "SpoolRecord",
    "resolve_spool_path",
]
