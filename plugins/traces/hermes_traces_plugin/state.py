"""Atomic, profile-scoped state for shared trace URLs."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import secrets
import tempfile
import time
from typing import Any, Callable, Iterator, Optional, TypeVar

_STATE_VERSION = 1
_MAX_SESSION_ID_CHARS = 512
_MAX_PLATFORM_CHARS = 64
_MAX_TRACE_HOME_CHARS = 4_096
_T = TypeVar("_T")


class State:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(".lock")

    @contextmanager
    def _lock(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        with self.lock_path.open("a+", encoding="utf-8") as lock_file:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"version": _STATE_VERSION, "sessions": {}}

    def _read_unlocked(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return self._empty()
        if (
            not isinstance(data, dict)
            or data.get("version") != _STATE_VERSION
            or not isinstance(data.get("sessions"), dict)
        ):
            return self._empty()
        return data

    def _write_unlocked(self, data: dict[str, Any]) -> None:
        fd, temporary = tempfile.mkstemp(prefix=".index-", dir=self.path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            directory_fd = os.open(self.path.parent, os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _read(self, callback: Callable[[dict[str, Any]], _T]) -> _T:
        with self._lock():
            return callback(self._read_unlocked())

    def _mutate(self, callback: Callable[[dict[str, Any]], _T]) -> _T:
        with self._lock():
            data = self._read_unlocked()
            result = callback(data)
            self._write_unlocked(data)
            return result

    @staticmethod
    def key(session_id: str, platform: str) -> str:
        return f"{platform.strip().lower()}:{session_id}"

    def create(
        self,
        session_id: str,
        platform: str = "discord",
        *,
        trace_home: Path | str | None = None,
    ) -> dict[str, Any]:
        session_id = str(session_id or "").strip()
        platform = str(platform or "").strip().lower()
        if not session_id or not platform:
            raise ValueError("session_id and platform are required")
        if len(session_id) > _MAX_SESSION_ID_CHARS or len(platform) > _MAX_PLATFORM_CHARS:
            raise ValueError("session_id or platform is too long")
        trace_home_text = str(trace_home or "").strip()
        if len(trace_home_text) > _MAX_TRACE_HOME_CHARS:
            raise ValueError("trace_home is too long")
        key = self.key(session_id, platform)

        def create_record(data: dict[str, Any]) -> dict[str, Any]:
            sessions = data.setdefault("sessions", {})
            record = sessions.get(key)
            if not isinstance(record, dict):
                record = {
                    "key": key,
                    "session_id": session_id,
                    "trace_id": session_id,
                    "platform": platform,
                    "slug": secrets.token_urlsafe(24),
                    "status": "pending",
                    "updated_at": time.time(),
                }
                sessions[key] = record
            if trace_home_text:
                record["trace_home"] = trace_home_text
            return dict(record)

        return self._mutate(create_record)

    def get(self, session_id: str, platform: str = "discord") -> Optional[dict[str, Any]]:
        return self.get_key(self.key(str(session_id), platform))

    def get_key(self, key: str) -> Optional[dict[str, Any]]:
        def read_record(data: dict[str, Any]) -> Optional[dict[str, Any]]:
            record = data.get("sessions", {}).get(key)
            return dict(record) if isinstance(record, dict) else None

        return self._read(read_record)

    def get_slug(self, slug: str) -> Optional[dict[str, Any]]:
        def find_record(data: dict[str, Any]) -> Optional[dict[str, Any]]:
            for record in data.get("sessions", {}).values():
                if isinstance(record, dict) and record.get("slug") == slug:
                    return dict(record)
            return None

        return self._read(find_record)

    def update(self, key: str, **changes: Any) -> Optional[dict[str, Any]]:
        changes = dict(changes)
        if changes.get("shared_url") is None:
            changes.pop("shared_url", None)

        def update_record(data: dict[str, Any]) -> Optional[dict[str, Any]]:
            record = data.setdefault("sessions", {}).get(key)
            if not isinstance(record, dict):
                return None
            record.update(changes)
            record["updated_at"] = time.time()
            return dict(record)

        return self._mutate(update_record)
