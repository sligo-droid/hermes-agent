"""Hermes lineage lookup and observer-only coding-worker trace storage."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Optional


_MAX_ID_CHARS = 512
_MAX_MESSAGES = 400
_MAX_CONTENT_CHARS = 100_000


def _clean_id(value: Any) -> str:
    text = str(value or "").strip()
    if (
        not text
        or len(text) > _MAX_ID_CHARS
        or any(ord(char) < 32 or ord(char) == 127 for char in text)
    ):
        return ""
    return text


class HermesStore:
    """Read native lineage and write a separate Hermes-shaped observer DB."""

    def __init__(self, hermes_home: Path | str, observer_home: Path | str):
        self.hermes_home = Path(hermes_home)
        self.main_db = self.hermes_home / "state.db"
        self.observer_home = Path(observer_home)
        self.observer_db = self.observer_home / "state.db"
        self._lock = threading.Lock()

    def resolve_root(
        self,
        session_id: Any,
        *,
        parent_session_id: Any = None,
        root_session_id: Any = None,
    ) -> str:
        explicit_root = _clean_id(root_session_id)
        if explicit_root:
            return explicit_root
        current = _clean_id(session_id)
        parent_hint = _clean_id(parent_session_id)
        if not current:
            return parent_hint
        if not self.main_db.is_file():
            return parent_hint or current
        try:
            connection = sqlite3.connect(
                f"file:{self.main_db}?mode=ro",
                uri=True,
                timeout=2,
            )
            try:
                seen: set[str] = set()
                for _ in range(32):
                    if not current or current in seen:
                        break
                    seen.add(current)
                    row = connection.execute(
                        "SELECT parent_session_id FROM sessions WHERE id = ?",
                        (current,),
                    ).fetchone()
                    if row is None:
                        return parent_hint or current
                    parent = _clean_id(row[0] if row else None)
                    if not parent:
                        return current
                    current = parent
            finally:
                connection.close()
        except (OSError, sqlite3.Error):
            return parent_hint or current
        return current or parent_hint

    def write_coding_worker(self, payload: dict[str, Any]) -> Optional[str]:
        """Persist one bounded trace outside Hermes' actionable session DB."""
        worker_id = _clean_id(payload.get("worker_session_id"))
        parent_id = _clean_id(payload.get("parent_session_id"))
        root_id = _clean_id(payload.get("root_session_id"))
        if not worker_id or not (parent_id or root_id):
            return None
        parent_id = parent_id or root_id
        started_at = self._timestamp(payload.get("started_at"), time.time())
        ended_at = self._timestamp(payload.get("ended_at"), None)
        status = str(payload.get("status") or "running")[:64]
        task = str(payload.get("task") or "")[:_MAX_CONTENT_CHARS]
        cwd = str(payload.get("cwd") or "")[:4_096]
        model = str(payload.get("model") or "")[:512]
        messages = self._messages(payload, task, started_at, ended_at, status)

        with self._lock:
            self.observer_home.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.observer_home, 0o700)
            connection = sqlite3.connect(self.observer_db, timeout=5)
            try:
                self._ensure_schema(connection)
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO sessions (
                        id, source, model, system_prompt, parent_session_id,
                        started_at, ended_at, end_reason, message_count,
                        tool_call_count, title
                    ) VALUES (?, 'tool', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        model=excluded.model,
                        system_prompt=excluded.system_prompt,
                        parent_session_id=excluded.parent_session_id,
                        started_at=excluded.started_at,
                        ended_at=excluded.ended_at,
                        end_reason=excluded.end_reason,
                        message_count=excluded.message_count,
                        tool_call_count=excluded.tool_call_count,
                        title=excluded.title
                    """,
                    (
                        worker_id,
                        model or None,
                        (
                            "Observer-only coding-worker trace. This is telemetry, "
                            "not an actionable Hermes conversation.\n"
                            f"Current working directory: {cwd}"
                        ),
                        parent_id,
                        started_at,
                        ended_at,
                        status if ended_at is not None else None,
                        len(messages),
                        sum(1 for item in messages if item.get("tool_calls")),
                        (task or "Delegated coding worker")[:240],
                    ),
                )
                connection.execute(
                    "DELETE FROM messages WHERE session_id = ?",
                    (worker_id,),
                )
                for message in messages:
                    connection.execute(
                        """
                        INSERT INTO messages (
                            session_id, role, content, tool_call_id, tool_calls,
                            tool_name, timestamp, reasoning, observed
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                        """,
                        (
                            worker_id,
                            message["role"],
                            message.get("content"),
                            message.get("tool_call_id"),
                            self._json(message.get("tool_calls")),
                            message.get("tool_name"),
                            message["timestamp"],
                            message.get("reasoning"),
                        ),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
            os.chmod(self.observer_db, 0o600)
        return worker_id

    @staticmethod
    def _timestamp(value: Any, default: Optional[float]) -> Optional[float]:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return default
        return parsed if parsed > 0 else default

    @staticmethod
    def _json(value: Any) -> Optional[str]:
        if value is None:
            return None
        try:
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            return None

    def _messages(
        self,
        payload: dict[str, Any],
        task: str,
        started_at: float,
        ended_at: Optional[float],
        status: str,
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": (
                    "[Observer-only coding worker input; this is telemetry and "
                    "must not be replayed as a new user request.]\n" + task
                )[:_MAX_CONTENT_CHARS],
                "timestamp": started_at,
            },
            {
                "role": "assistant",
                "content": (
                    "Coding worker started"
                    f" (backend={str(payload.get('backend') or 'unknown')[:64]}, "
                    f"worker_session_id={str(payload.get('worker_session_id') or '')[:128]})."
                ),
                "timestamp": started_at + 0.000001,
            },
        ]
        worker_messages = payload.get("worker_messages")
        if isinstance(worker_messages, list) and worker_messages:
            tool_names_by_call: dict[str, str] = {}
            for raw in worker_messages[:_MAX_MESSAGES]:
                normalized = self._normalize_message(raw, tool_names_by_call)
                if normalized is not None:
                    messages.append(normalized)
        elif isinstance(payload.get("worker_events"), list):
            tool_names_by_call = {}
            emitted_calls: set[str] = set()
            emitted_results: set[str] = set()
            for index, event in enumerate(payload["worker_events"][:_MAX_MESSAGES]):
                if not isinstance(event, dict):
                    continue
                native_messages = self._normalize_native_event(
                    event,
                    index=index,
                    tool_names_by_call=tool_names_by_call,
                    emitted_calls=emitted_calls,
                    emitted_results=emitted_results,
                )
                if native_messages:
                    messages.extend(native_messages)
                    continue
                kind = str(event.get("type") or event.get("method") or "event")[:128]
                encoded = self._json(event) or "{}"
                messages.append(
                    {
                        "role": "assistant",
                        "content": f"[coding worker {kind}] {encoded}"[:_MAX_CONTENT_CHARS],
                    }
                )
        if ended_at is not None:
            summary = str(payload.get("summary") or "")[:20_000]
            error = str(payload.get("error") or "")[:4_000]
            closeout = (
                f"Coding worker {status}; duration_ms={int(payload.get('duration_ms') or 0)}; "
                f"thread_id={str(payload.get('thread_id') or '')[:256]}; "
                f"turn_id={str(payload.get('turn_id') or '')[:256]}."
            )
            if summary:
                closeout += "\n\nSummary:\n" + summary
            if error:
                closeout += "\n\nError:\n" + error
            messages.append(
                {
                    "role": "assistant",
                    "content": closeout[:_MAX_CONTENT_CHARS],
                    "timestamp": ended_at,
                }
            )
        span = max((ended_at or started_at) - started_at, 0.001)
        for index, message in enumerate(messages):
            message.setdefault(
                "timestamp",
                started_at + span * (index + 1) / (len(messages) + 1),
            )
        return messages[: _MAX_MESSAGES + 3]

    @staticmethod
    def _normalize_message(
        raw: Any,
        tool_names_by_call: Optional[dict[str, str]] = None,
    ) -> Optional[dict[str, Any]]:
        if not isinstance(raw, dict):
            return None
        role = str(raw.get("role") or "").strip()
        if role not in {"user", "assistant", "tool"}:
            return None
        message: dict[str, Any] = {
            "role": role,
            "content": (
                None
                if raw.get("content") is None
                else str(raw.get("content"))[:_MAX_CONTENT_CHARS]
            ),
        }
        tool_call_id = _clean_id(raw.get("tool_call_id"))
        if tool_call_id:
            message["tool_call_id"] = tool_call_id
        reasoning = raw.get("reasoning")
        if isinstance(reasoning, str):
            message["reasoning"] = reasoning[:_MAX_CONTENT_CHARS]
        if isinstance(raw.get("tool_calls"), list):
            calls: list[dict[str, Any]] = []
            for raw_call in raw["tool_calls"][:64]:
                if not isinstance(raw_call, dict):
                    continue
                call_id = _clean_id(raw_call.get("id"))
                function = raw_call.get("function")
                function = function if isinstance(function, dict) else {}
                tool_name = str(function.get("name") or "")[:512]
                arguments = function.get("arguments")
                if not isinstance(arguments, str):
                    try:
                        arguments = json.dumps(
                            arguments or {},
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                    except (TypeError, ValueError):
                        arguments = "{}"
                call = {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": arguments[:_MAX_CONTENT_CHARS],
                    },
                }
                calls.append(call)
                if tool_names_by_call is not None and call_id and tool_name:
                    tool_names_by_call[call_id] = tool_name
            if calls:
                message["tool_calls"] = calls
        if role == "tool":
            tool_name = str(raw.get("tool_name") or raw.get("name") or "")[:512]
            if not tool_name and tool_names_by_call is not None and tool_call_id:
                tool_name = tool_names_by_call.get(tool_call_id, "")
            message["tool_name"] = tool_name or "tool"
        return message

    @classmethod
    def _normalize_native_event(
        cls,
        event: dict[str, Any],
        *,
        index: int,
        tool_names_by_call: dict[str, str],
        emitted_calls: set[str],
        emitted_results: set[str],
    ) -> list[dict[str, Any]]:
        """Convert common OpenCode tool updates into Hermes-shaped messages."""
        kind = str(event.get("type") or event.get("method") or "").strip().lower()
        properties = event.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        part = event.get("part") or properties.get("part")
        part = part if isinstance(part, dict) else {}
        part_type = str(part.get("type") or "").strip().lower()
        if "tool" not in kind and "tool" not in part_type:
            return []

        state = part.get("state")
        state = state if isinstance(state, dict) else {}
        call_id = _clean_id(
            part.get("callID")
            or part.get("callId")
            or part.get("call_id")
            or event.get("tool_call_id")
            or event.get("callID")
            or event.get("call_id")
        ) or f"opencode-tool-{index}"
        tool_name = str(
            part.get("tool")
            or part.get("name")
            or event.get("tool")
            or event.get("tool_name")
            or event.get("name")
            or tool_names_by_call.get(call_id)
            or "tool"
        )[:512]
        tool_names_by_call[call_id] = tool_name
        arguments = (
            state.get("input")
            if "input" in state
            else part.get("input", event.get("input", event.get("arguments", {})))
        )
        if not isinstance(arguments, str):
            try:
                arguments = json.dumps(
                    arguments or {},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            except (TypeError, ValueError):
                arguments = "{}"

        normalized: list[dict[str, Any]] = []
        result_only = kind in {"tool_result", "tool.result", "tool-result"}
        if call_id not in emitted_calls:
            normalized.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": arguments[:_MAX_CONTENT_CHARS],
                            },
                        }
                    ],
                }
            )
            emitted_calls.add(call_id)

        status = str(state.get("status") or event.get("status") or "").lower()
        terminal = result_only or status in {
            "completed",
            "complete",
            "done",
            "error",
            "failed",
            "cancelled",
        }
        output = (
            state.get("output")
            if "output" in state
            else state.get("result")
            if "result" in state
            else state.get("error")
            if "error" in state
            else event.get("output", event.get("result", event.get("error")))
        )
        if terminal and call_id not in emitted_results:
            if not isinstance(output, str):
                try:
                    output = json.dumps(
                        output if output is not None else {"status": status or "completed"},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                except (TypeError, ValueError):
                    output = status or "completed"
            normalized.append(
                {
                    "role": "tool",
                    "content": str(output)[:_MAX_CONTENT_CHARS],
                    "tool_call_id": call_id,
                    "tool_name": tool_name,
                }
            )
            emitted_results.add(call_id)
        return normalized

    @staticmethod
    def _ensure_schema(connection: sqlite3.Connection) -> None:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                user_id TEXT,
                model TEXT,
                model_config TEXT,
                system_prompt TEXT,
                parent_session_id TEXT,
                started_at REAL NOT NULL,
                ended_at REAL,
                end_reason TEXT,
                message_count INTEGER DEFAULT 0,
                tool_call_count INTEGER DEFAULT 0,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                cache_read_tokens INTEGER DEFAULT 0,
                cache_write_tokens INTEGER DEFAULT 0,
                reasoning_tokens INTEGER DEFAULT 0,
                billing_provider TEXT,
                billing_base_url TEXT,
                billing_mode TEXT,
                estimated_cost_usd REAL,
                actual_cost_usd REAL,
                cost_status TEXT,
                cost_source TEXT,
                pricing_version TEXT,
                title TEXT,
                api_call_count INTEGER DEFAULT 0,
                handoff_state TEXT,
                handoff_platform TEXT,
                handoff_error TEXT,
                transcript_revision INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                tool_call_id TEXT,
                tool_calls TEXT,
                tool_name TEXT,
                timestamp REAL NOT NULL,
                token_count INTEGER,
                finish_reason TEXT,
                reasoning TEXT,
                reasoning_content TEXT,
                reasoning_details TEXT,
                codex_reasoning_items TEXT,
                codex_message_items TEXT,
                platform_message_id TEXT,
                observed INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_messages_session
                ON messages(session_id, timestamp, id);
            """
        )
