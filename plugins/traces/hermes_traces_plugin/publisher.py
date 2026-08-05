"""Fail-open asynchronous publication through the official Traces CLI."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from collections import deque
from typing import Any, Optional
from urllib.parse import urlsplit

from .config import Config
from .state import State

_MAX_STDOUT_CHARS = 65_536
_MAX_SHARED_URL_CHARS = 2_048
_MAX_REMOTE_TRACE_ID_CHARS = 512
_ALLOWED_TRACE_HOSTS = frozenset({"traces.com", "www.traces.com"})


class Publisher:
    def __init__(self, config: Config, state: State, *, start_worker: bool = True):
        self.config = config
        self.state = state
        self._pending: deque[str] = deque()
        self._pending_keys: set[str] = set()
        self._condition = threading.Condition()
        self._worker: Optional[threading.Thread] = None
        if start_worker:
            self._worker = threading.Thread(
                target=self._work,
                daemon=True,
                name="hermes-traces-publisher",
            )
            self._worker.start()

    def enqueue(self, key: str) -> None:
        self.enqueue_many([key])

    def enqueue_many(self, keys: list[str]) -> None:
        added = False
        with self._condition:
            for key in keys:
                if not key:
                    continue
                if key in self._pending_keys:
                    try:
                        self._pending.remove(key)
                    except ValueError:
                        pass
                    else:
                        self._pending_keys.discard(key)
                self._pending.append(key)
                self._pending_keys.add(key)
                added = True
            if added:
                self._condition.notify()

    def _work(self) -> None:
        while True:
            with self._condition:
                while not self._pending:
                    self._condition.wait()
                key = self._pending.popleft()
                self._pending_keys.discard(key)
            try:
                self.publish(key)
            except Exception:
                self.state.update(key, status="error", error="unexpected_error")

    @staticmethod
    def _valid_url(url: Any) -> bool:
        if not isinstance(url, str) or not url or len(url) > _MAX_SHARED_URL_CHARS:
            return False
        if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url):
            return False
        try:
            parsed = urlsplit(url)
            return (
                parsed.scheme.lower() == "https"
                and (parsed.hostname or "").lower() in _ALLOWED_TRACE_HOSTS
                and parsed.username is None
                and parsed.password is None
                and parsed.port in (None, 443)
            )
        except (TypeError, ValueError):
            return False

    def _record_error(self, key: str, code: str) -> Optional[dict[str, Any]]:
        record = self.state.get_key(key)
        has_last_good_url = bool(
            record
            and record.get("visibility") == "private"
            and self._valid_url(record.get("shared_url"))
        )
        return self.state.update(
            key,
            status="ready" if has_last_good_url else "error",
            error=code[:64],
        )

    def publish(self, key: str) -> Optional[dict[str, Any]]:
        record = self.state.get_key(key)
        if not record:
            return None

        local_trace_id = str(record.get("session_id") or "").strip()
        if not local_trace_id:
            return self._record_error(key, "invalid_local_trace_id")

        refreshing = bool(record.get("shared_url"))
        if refreshing:
            command = [
                self.config.executable,
                "refresh",
                "--trace-id",
                local_trace_id,
                "--json",
            ]
        else:
            command = [
                self.config.executable,
                "share",
                "--trace-id",
                local_trace_id,
                "--agent",
                "hermes",
                "--visibility",
                "private",
                "--json",
            ]

        environment = os.environ.copy()
        environment["HERMES_HOME"] = str(self.config.hermes_home)
        trace_home = str(record.get("trace_home") or "").strip()
        environment["TRACES_HERMES_DIR"] = trace_home or str(self.config.hermes_home)

        try:
            result = subprocess.run(
                command,
                shell=False,
                timeout=self.config.timeout,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode:
                return self._record_error(key, "command_failed")
            if len(result.stdout) > _MAX_STDOUT_CHARS:
                return self._record_error(key, "output_too_large")
            payload = json.loads(result.stdout)
            if not isinstance(payload, dict) or payload.get("ok") is not True:
                return self._record_error(key, "invalid_response")
            data = payload.get("data")
            if not isinstance(data, dict):
                return self._record_error(key, "invalid_response")

            shared_url = data.get("sharedUrl", record.get("shared_url"))
            visibility = data.get("visibility", record.get("visibility"))
            remote_trace_id = data.get("traceId", record.get("remote_trace_id"))
            if visibility != "private" or not self._valid_url(shared_url):
                return self._record_error(key, "invalid_response")
            if remote_trace_id is not None and (
                not isinstance(remote_trace_id, str)
                or len(remote_trace_id) > _MAX_REMOTE_TRACE_ID_CHARS
            ):
                return self._record_error(key, "invalid_response")
            if not refreshing and (
                not isinstance(data.get("sharedUrl"), str)
                or data.get("visibility") != "private"
            ):
                return self._record_error(key, "invalid_response")

            return self.state.update(
                key,
                status="ready",
                remote_trace_id=remote_trace_id,
                shared_url=shared_url,
                visibility="private",
                error=None,
            )
        except subprocess.TimeoutExpired:
            return self._record_error(key, "timeout")
        except json.JSONDecodeError:
            return self._record_error(key, "invalid_json")
        except OSError:
            return self._record_error(key, "execution_failed")
        except Exception:
            return self._record_error(key, "unexpected_error")
