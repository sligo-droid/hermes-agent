"""MemoryManager — orchestrates memory providers for the agent.

Single integration point in run_agent.py. Replaces scattered per-backend
code with one manager that delegates to registered providers.

Only ONE external plugin provider is allowed at a time — attempting to
register a second external provider is rejected with a warning.  This
prevents tool schema bloat and conflicting memory backends.

Usage in run_agent.py:
    self._memory_manager = MemoryManager()
    # Only ONE of these:
    self._memory_manager.add_provider(plugin_provider)

    # System prompt
    prompt_parts.append(self._memory_manager.build_system_prompt())

    # Pre-turn
    context = self._memory_manager.prefetch_all(user_message)

    # Post-turn
    self._memory_manager.sync_all(user_msg, assistant_response)
    self._memory_manager.queue_prefetch_all(user_msg)
"""

from __future__ import annotations

import inspect
import json
import logging
import re
import threading
from concurrent.futures import Future, ThreadPoolExecutor, wait
from typing import Any, Callable, Dict, List, Optional

from agent.memory_provider import MemoryProvider
from agent.skill_commands import extract_user_instruction_from_skill_message
from tools.registry import tool_error

logger = logging.getLogger(__name__)


# How long shutdown_all() waits for in-flight background sync/prefetch work
# to drain before abandoning it. A wedged provider must never block process
# teardown indefinitely — the worker threads are daemon, so anything still
# running past this window dies with the interpreter.
_SYNC_DRAIN_TIMEOUT_S = 5.0
_EXTERNAL_PREFETCH_TIMEOUT_S = 8.0

_WRITE_TOOL_RE = re.compile(
    r"(?:^|[^A-Za-z0-9])(?:retain|remember|conclude|add|create|update|delete|remove|forget|write|ingest|upload)(?:$|[^A-Za-z0-9])",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Context fencing helpers
# ---------------------------------------------------------------------------

_FENCE_TAG_RE = re.compile(r'</?\s*memory-context\s*>', re.IGNORECASE)
_INTERNAL_CONTEXT_RE = re.compile(
    r'<\s*memory-context\s*>[\s\S]*?</\s*memory-context\s*>',
    re.IGNORECASE,
)
_INTERNAL_NOTE_RE = re.compile(
    r'\[System note:\s*The following is recalled memory context,\s*NOT new user input\.\s*Treat as (?:informational background data|authoritative reference data[^\]]*)\.\]\s*',
    re.IGNORECASE,
)

_MEMORY_TEXT_PART_TYPES = {"text", "input_text", "output_text", "refusal"}
_MEMORY_TEXT_PART_VALUE_KEYS = ("text", "content", "refusal")
_MEMORY_NESTED_TEXT_PART_TYPES = {"message", "input_message", "output_message"}
_MEMORY_MEDIA_PART_TYPES = {
    "audio",
    "binary",
    "document",
    "file",
    "image",
    "image_url",
    "input_audio",
    "input_file",
    "input_image",
    "video",
}
_MEMORY_PART_TYPE_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_OMITTED_MEMORY_CONTENT_RE = re.compile(
    r"(?:\[omitted non-text memory content: [^\]\n]+\](?:\n|$))+"
)


def sanitize_context(text: str) -> str:
    """Strip fence tags, injected context blocks, and system notes from provider output."""
    text = _INTERNAL_CONTEXT_RE.sub('', text)
    text = _INTERNAL_NOTE_RE.sub('', text)
    text = _FENCE_TAG_RE.sub('', text)
    return text


def _safe_memory_part_type(raw_type: Any) -> str:
    """Return bounded structural metadata for an omitted content part."""
    part_type = str(raw_type or "unknown").strip().lower() or "unknown"
    part_type = _MEMORY_PART_TYPE_RE.sub("_", part_type)
    return part_type[:40] or "unknown"


def _looks_textual(text: str) -> bool:
    """Reject decoded bytes that still look like binary/control payloads."""
    return not any(
        (ord(ch) < 32 and ch not in "\t\n\r") or ord(ch) == 127
        for ch in text
    )


def _decode_textual_bytes(data: bytes) -> Optional[str]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not _looks_textual(text):
        return None
    return text


def _format_omitted_memory_parts(omitted: Dict[str, int]) -> str:
    summary = ", ".join(f"{part_type}={count}" for part_type, count in omitted.items())
    return f"[omitted non-text memory content: {summary}]"


def normalize_memory_turn_content(content: Any) -> str:
    """Convert completed-turn content into safe provider-facing text.

    Runtime messages can be plain strings, bytes, or OpenAI-style multimodal
    part lists. Memory providers historically accept text, so this extracts
    text parts in order and replaces non-text/binary/media parts with bounded
    type/count markers that do not include raw URLs, data URLs, or payloads.
    """
    parts: list[str] = []
    omitted: Dict[str, int] = {}

    def flush_omitted() -> None:
        if omitted:
            parts.append(_format_omitted_memory_parts(omitted))
            omitted.clear()

    def add_text(text: str) -> None:
        if text:
            flush_omitted()
            parts.append(text)

    def omit(part_type: Any) -> None:
        safe_type = _safe_memory_part_type(part_type)
        omitted[safe_type] = omitted.get(safe_type, 0) + 1

    def walk(value: Any) -> None:
        if value is None:
            return
        if isinstance(value, str):
            add_text(value)
            return
        if isinstance(value, bytes):
            text = _decode_textual_bytes(value)
            if text is None:
                omit("bytes")
            else:
                add_text(text)
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                walk(item)
            return
        if isinstance(value, dict):
            raw_type = value.get("type")
            part_type = _safe_memory_part_type(raw_type)
            if part_type in _MEMORY_TEXT_PART_TYPES:
                for key in _MEMORY_TEXT_PART_VALUE_KEYS:
                    if key in value:
                        walk(value.get(key))
                        break
                return
            if not raw_type and "text" in value:
                walk(value.get("text"))
                return
            if part_type in _MEMORY_NESTED_TEXT_PART_TYPES and "content" in value:
                walk(value.get("content"))
                return
            if part_type in _MEMORY_MEDIA_PART_TYPES:
                omit(part_type)
                return
            omit(part_type)
            return
        omit(type(value).__name__)

    walk(content)
    flush_omitted()
    return "\n".join(parts)


def _has_recoverable_memory_text(content: str) -> bool:
    """Return whether normalized content contains more than omission markers."""
    stripped = content.strip()
    return bool(stripped and not _OMITTED_MEMORY_CONTENT_RE.fullmatch(stripped))


class StreamingContextScrubber:
    """Stateful scrubber for streaming text that may contain split memory-context spans.

    The one-shot ``sanitize_context`` regex cannot survive chunk boundaries:
    a ``<memory-context>`` opened in one delta and closed in a later delta
    leaks its payload to the UI because the non-greedy block regex needs
    both tags in one string.  This scrubber runs a small state machine
    across deltas, holding back partial-tag tails and discarding
    everything inside a span (including the system-note line).

    Usage::

        scrubber = StreamingContextScrubber()
        for delta in stream:
            visible = scrubber.feed(delta)
            if visible:
                emit(visible)
        trailing = scrubber.flush()  # at end of stream
        if trailing:
            emit(trailing)

    The scrubber is re-entrant per agent instance.  Callers building new
    top-level responses (new turn) should create a fresh scrubber or call
    ``reset()``.
    """

    _OPEN_TAG = "<memory-context>"
    _CLOSE_TAG = "</memory-context>"

    def __init__(self) -> None:
        self._in_span: bool = False
        self._buf: str = ""
        self._at_block_boundary: bool = True

    def reset(self) -> None:
        self._in_span = False
        self._buf = ""
        self._at_block_boundary = True

    def feed(self, text: str) -> str:
        """Return the visible portion of ``text`` after scrubbing.

        Any trailing fragment that could be the start of an open/close tag
        is held back in the internal buffer and surfaced on the next
        ``feed()`` call or discarded/emitted by ``flush()``.
        """
        if not text:
            return ""
        buf = self._buf + text
        self._buf = ""
        out: list[str] = []

        while buf:
            if self._in_span:
                idx = buf.lower().find(self._CLOSE_TAG)
                if idx == -1:
                    # Hold back a potential partial close tag; drop the rest
                    held = self._max_partial_suffix(buf, self._CLOSE_TAG)
                    self._buf = buf[-held:] if held else ""
                    return "".join(out)
                # Found close — skip span content + tag, continue
                buf = buf[idx + len(self._CLOSE_TAG):]
                self._in_span = False
            else:
                idx = self._find_boundary_open_tag(buf)
                if idx == -1:
                    # No open tag — hold back a potential partial open tag
                    held = (
                        self._max_pending_open_suffix(buf)
                        or self._max_partial_suffix(buf, self._OPEN_TAG)
                    )
                    if held:
                        self._append_visible(out, buf[:-held])
                        self._buf = buf[-held:]
                    else:
                        self._append_visible(out, buf)
                    return "".join(out)
                # Emit text before the tag, enter span
                if idx > 0:
                    self._append_visible(out, buf[:idx])
                buf = buf[idx + len(self._OPEN_TAG):]
                self._in_span = True

        return "".join(out)

    def flush(self) -> str:
        """Emit any held-back buffer at end-of-stream.

        If we're still inside an unterminated span the remaining content is
        discarded (safer: leaking partial memory context is worse than a
        truncated answer).  Otherwise the held-back partial-tag tail is
        emitted verbatim (it turned out not to be a real tag).
        """
        if self._in_span:
            self._buf = ""
            self._in_span = False
            return ""
        tail = self._buf
        self._buf = ""
        return tail

    @staticmethod
    def _max_partial_suffix(buf: str, tag: str) -> int:
        """Return the length of the longest buf-suffix that is a tag-prefix.

        Case-insensitive.  Returns 0 if no suffix could start the tag.
        """
        tag_lower = tag.lower()
        buf_lower = buf.lower()
        max_check = min(len(buf_lower), len(tag_lower) - 1)
        for i in range(max_check, 0, -1):
            if tag_lower.startswith(buf_lower[-i:]):
                return i
        return 0

    def _find_boundary_open_tag(self, buf: str) -> int:
        """Find an opening fence only when it starts a block-like span."""
        buf_lower = buf.lower()
        search_start = 0
        while True:
            idx = buf_lower.find(self._OPEN_TAG, search_start)
            if idx == -1:
                return -1
            if self._is_block_boundary(buf, idx) and self._has_block_opener_suffix(buf, idx):
                return idx
            search_start = idx + 1

    def _max_pending_open_suffix(self, buf: str) -> int:
        """Hold a complete boundary tag until the following char confirms it."""
        if not buf.lower().endswith(self._OPEN_TAG):
            return 0
        idx = len(buf) - len(self._OPEN_TAG)
        if not self._is_block_boundary(buf, idx):
            return 0
        return len(self._OPEN_TAG)

    def _has_block_opener_suffix(self, buf: str, idx: int) -> bool:
        after_idx = idx + len(self._OPEN_TAG)
        if after_idx >= len(buf):
            return False
        return buf[after_idx] in "\r\n"

    def _is_block_boundary(self, buf: str, idx: int) -> bool:
        if idx == 0:
            return self._at_block_boundary
        preceding = buf[:idx]
        last_newline = preceding.rfind("\n")
        if last_newline == -1:
            return self._at_block_boundary and preceding.strip() == ""
        return preceding[last_newline + 1:].strip() == ""

    def _append_visible(self, out: list[str], text: str) -> None:
        if not text:
            return
        out.append(text)
        self._update_block_boundary(text)

    def _update_block_boundary(self, text: str) -> None:
        last_newline = text.rfind("\n")
        if last_newline != -1:
            self._at_block_boundary = text[last_newline + 1:].strip() == ""
        else:
            self._at_block_boundary = self._at_block_boundary and text.strip() == ""


def build_memory_context_block(raw_context: str) -> str:
    """Wrap prefetched memory in a fenced block with system note."""
    if not raw_context or not raw_context.strip():
        return ""
    clean = sanitize_context(raw_context)
    if clean != raw_context:
        logger.warning("memory provider returned pre-wrapped context; stripped")
    return (
        "<memory-context>\n"
        "[System note: The following is recalled memory context, "
        "NOT new user input. Treat as authoritative reference data — "
        "this is the agent's persistent memory and should inform all responses. "
        "If this block says it was truncated, use explicit memory/retrieval "
        "tools for full source content.]\n\n"
        f"{clean}\n"
        "</memory-context>"
    )


def normalize_tool_schema(schema: Any) -> Optional[Dict[str, Any]]:
    """Return a bare function schema or ``None`` for malformed input.

    Memory providers and context engines are expected to return a bare OpenAI
    function schema (``{"name": ..., "parameters": ...}``), but some return an
    already-wrapped tool entry (``{"type": "function", "function": {...}}``).
    Normalize that single wrapper and reject nameless/double-wrapped shapes so
    one bad provider schema cannot poison the whole tool surface.
    """
    if not isinstance(schema, dict):
        return None
    candidate = schema
    if schema.get("type") == "function" and isinstance(schema.get("function"), dict):
        candidate = schema["function"]
    if candidate.get("type") == "function" and "function" in candidate:
        return None
    name = candidate.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    return candidate


def memory_provider_tools_enabled(enabled_toolsets: Optional[List[str]]) -> bool:
    """Return whether the selected toolsets expose the core memory tool."""
    if enabled_toolsets is None:
        return True
    if not enabled_toolsets:
        return False
    if "memory" in enabled_toolsets:
        return True

    try:
        from toolsets import resolve_toolset

        return any("memory" in resolve_toolset(name) for name in enabled_toolsets)
    except Exception:
        logger.debug(
            "Failed to resolve enabled toolsets for memory-provider tools",
            exc_info=True,
        )
        return False


def inject_memory_provider_tools(agent: Any) -> int:
    """Append normalized memory-provider tool schemas to an agent-like object."""
    manager = getattr(agent, "_memory_manager", None)
    if not manager or getattr(agent, "tools", None) is None:
        return 0

    existing_tool_names = {
        t.get("function", {}).get("name")
        for t in agent.tools
        if isinstance(t, dict)
    }
    if (
        "memory" not in existing_tool_names
        and not memory_provider_tools_enabled(
            getattr(agent, "enabled_toolsets", None)
        )
    ):
        return 0

    get_schemas = getattr(manager, "get_all_tool_schemas", None)
    if not callable(get_schemas):
        return 0

    valid_tool_names = getattr(agent, "valid_tool_names", None)
    if valid_tool_names is None:
        valid_tool_names = set()
        agent.valid_tool_names = valid_tool_names

    added = 0
    for schema in get_schemas():
        normalized = normalize_tool_schema(schema)
        if normalized is None:
            continue
        tool_name = normalized.get("name", "")
        if tool_name in existing_tool_names:
            continue
        agent.tools.append({"type": "function", "function": normalized})
        valid_tool_names.add(tool_name)
        existing_tool_names.add(tool_name)
        added += 1
    return added


class MemoryManager:
    """Orchestrates the built-in provider plus at most one external provider.

    The builtin provider is always first. Only one non-builtin (external)
    provider is allowed.  Failures in one provider never block the other.
    """


    def __init__(self, *, read_only: bool = False, external_prefetch_timeout: Optional[float] = None) -> None:
        self._providers: List[MemoryProvider] = []
        self._tool_to_provider: Dict[str, MemoryProvider] = {}
        self._has_external: bool = False  # True once a non-builtin provider is added
        self._external_prefetch_timeout = (
            _EXTERNAL_PREFETCH_TIMEOUT_S
            if external_prefetch_timeout is None
            else float(external_prefetch_timeout)
        )
        if self._external_prefetch_timeout <= 0:
            raise ValueError("external_prefetch_timeout must be positive")
        self._external_prefetch_threads: Dict[str, threading.Thread] = {}
        self._external_prefetch_lock = threading.Lock()
        # Background executor for end-of-turn sync/prefetch. Lazily created on
        # first use so the common builtin-only path spawns no extra threads.
        # A single worker serializes a provider's writes (turn N must land
        # before turn N+1) and caps thread growth at one per manager. See
        # _submit_background() and the sync_all/queue_prefetch_all rationale.
        self._sync_executor: Optional[ThreadPoolExecutor] = None
        self._sync_executor_lock = threading.Lock()
        # Futures are tracked by durability class so shutdown can give writes
        # a bounded FIFO drain, then explicitly report anything abandoned.
        self._background_futures: Dict[Future, str] = {}
        self._shutting_down = False
        self._shutdown_drain_state: Dict[str, Any] = {
            "status": "not_started",
            "abandoned_writes": 0,
            "abandoned_prefetches": 0,
            "active_tasks": 0,
        }
        self.read_only = bool(read_only)

    @staticmethod
    def is_write_like_tool_schema(schema: Dict[str, Any]) -> bool:
        """Best-effort filter for provider tools that appear to write memory."""
        name = str(schema.get("name") or "")
        description = str(schema.get("description") or "")
        return bool(_WRITE_TOOL_RE.search(f"{name} {description}"))

    @staticmethod
    def is_reserved_tool_name(tool_name: str) -> bool:
        """Return True when a memory provider tool would shadow a core tool."""
        if not tool_name:
            return False
        try:
            from toolsets import _HERMES_CORE_TOOLS

            return tool_name in set(_HERMES_CORE_TOOLS)
        except Exception:
            return tool_name in {"clarify", "delegate_task", "memory", "session_search"}

    # -- Registration --------------------------------------------------------

    def add_provider(self, provider: MemoryProvider) -> None:
        """Register a memory provider.

        Built-in provider (name ``"builtin"``) is always accepted.
        Only **one** external (non-builtin) provider is allowed — a second
        attempt is rejected with a warning.
        """
        is_builtin = provider.name == "builtin"

        if not is_builtin:
            if self._has_external:
                existing = next(
                    (p.name for p in self._providers if p.name != "builtin"), "unknown"
                )
                logger.warning(
                    "Rejected memory provider '%s' — external provider '%s' is "
                    "already registered. Only one external memory provider is "
                    "allowed at a time. Configure which one via memory.provider "
                    "in config.yaml.",
                    provider.name, existing,
                )
                return
            self._has_external = True

        self._providers.append(provider)

        # Index tool names → provider for routing
        for raw_schema in provider.get_tool_schemas():
            schema = normalize_tool_schema(raw_schema)
            if schema is None:
                continue
            if self.read_only and self.is_write_like_tool_schema(schema):
                continue
            tool_name = schema.get("name", "")
            if self.is_reserved_tool_name(tool_name):
                logger.warning(
                    "Rejected memory tool '%s' from %s — reserved core tool name",
                    tool_name,
                    provider.name,
                )
                continue
            if tool_name and tool_name not in self._tool_to_provider:
                self._tool_to_provider[tool_name] = provider
            elif tool_name in self._tool_to_provider:
                logger.warning(
                    "Memory tool name conflict: '%s' already registered by %s, "
                    "ignoring from %s",
                    tool_name,
                    self._tool_to_provider[tool_name].name,
                    provider.name,
                )

        logger.info(
            "Memory provider '%s' registered (%d tools)",
            provider.name,
            len(provider.get_tool_schemas()),
        )

    @property
    def providers(self) -> List[MemoryProvider]:
        """All registered providers in order."""
        return list(self._providers)

    def get_provider(self, name: str) -> Optional[MemoryProvider]:
        """Get a provider by name, or None if not registered."""
        for p in self._providers:
            if p.name == name:
                return p
        return None

    # -- System prompt -------------------------------------------------------

    def build_system_prompt(self) -> str:
        """Collect system prompt blocks from all providers.

        Returns combined text, or empty string if no providers contribute.
        Each non-empty block is labeled with the provider name.
        """
        blocks = []
        for provider in self._providers:
            try:
                block = provider.system_prompt_block()
                if block and block.strip():
                    blocks.append(block)
            except Exception as e:
                logger.warning(
                    "Memory provider '%s' system_prompt_block() failed: %s",
                    provider.name, e,
                )
        return "\n\n".join(blocks)

    # -- Prefetch / recall ---------------------------------------------------

    @staticmethod
    def _strip_skill_scaffolding(text: str) -> Optional[str]:
        """Return the user's actual instruction from an expanded skill turn."""
        return extract_user_instruction_from_skill_message(text)

    def prefetch_all(self, query: Any, *, session_id: str = "") -> str:
        """Collect prefetch context from all providers.

        Returns merged context text labeled by provider. Empty providers
        are skipped. Failures in one provider don't block others.
        """
        normalized_query = normalize_memory_turn_content(query)
        if not _has_recoverable_memory_text(normalized_query):
            return ""
        clean_query = self._strip_skill_scaffolding(normalized_query)
        if not clean_query:
            return ""
        parts = []
        for provider in self._providers:
            try:
                result = self._prefetch_provider(provider, clean_query, session_id=session_id)
                if result and result.strip():
                    parts.append(
                        f"## Memory provider: {provider.name}\n\n{result.strip()}"
                    )
            except Exception as e:
                logger.debug(
                    "Memory provider '%s' prefetch failed (non-fatal): %s",
                    provider.name, e,
                )
        return "\n\n".join(parts)

    def _prefetch_provider(
        self, provider: MemoryProvider, query: str, *, session_id: str = ""
    ) -> str:
        if provider.name == "builtin":
            return provider.prefetch(query, session_id=session_id)

        result_box: Dict[str, str] = {}
        error_box: Dict[str, Exception] = {}

        def _run() -> None:
            try:
                result_box["value"] = provider.prefetch(query, session_id=session_id) or ""
            except Exception as exc:  # pragma: no cover - re-raised by caller
                error_box["value"] = exc

        thread = threading.Thread(
            target=_run,
            daemon=True,
            name=f"memory-prefetch-{provider.name}",
        )
        with self._external_prefetch_lock:
            existing = self._external_prefetch_threads.get(provider.name)
            if existing is not None:
                if existing.is_alive():
                    logger.debug(
                        "Memory provider '%s' prefetch is still running; skipping this turn",
                        provider.name,
                    )
                    return ""
                self._external_prefetch_threads.pop(provider.name, None)
            self._external_prefetch_threads[provider.name] = thread
            thread.start()

        thread.join(self._external_prefetch_timeout)
        if thread.is_alive():
            logger.warning(
                "Memory provider '%s' prefetch timed out after %.1fs; skipping it until "
                "the stuck call returns",
                provider.name,
                self._external_prefetch_timeout,
            )
            return ""

        with self._external_prefetch_lock:
            if self._external_prefetch_threads.get(provider.name) is thread:
                self._external_prefetch_threads.pop(provider.name, None)
        if error_box:
            raise error_box["value"]
        return result_box.get("value", "")

    def queue_prefetch_all(self, query: Any, *, session_id: str = "") -> None:

        """Queue background prefetch on all providers for the next turn.

        Provider work is dispatched to a background worker so a slow or
        wedged provider can never block the caller. See ``sync_all`` for
        the full rationale (agent stuck "running" minutes after a turn).
        """
        providers = list(self._providers)
        if not providers:
            return

        normalized_query = normalize_memory_turn_content(query)
        if not _has_recoverable_memory_text(normalized_query):
            return
        clean_query = self._strip_skill_scaffolding(normalized_query)
        if not clean_query:
            return

        def _run() -> None:
            for provider in providers:
                try:
                    provider.queue_prefetch(clean_query, session_id=session_id)
                except Exception as e:
                    logger.debug(
                        "Memory provider '%s' queue_prefetch failed (non-fatal): %s",
                        provider.name, e,
                    )

        self._submit_background(_run, kind="prefetch")

    # -- Sync ----------------------------------------------------------------

    @staticmethod
    def _provider_sync_accepts_messages(provider: MemoryProvider) -> bool:
        """Return whether sync_turn accepts a messages keyword."""
        try:
            signature = inspect.signature(provider.sync_turn)
        except (TypeError, ValueError):
            return True
        params = list(signature.parameters.values())
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params):
            return True
        return "messages" in signature.parameters

    def sync_all(
        self,
        user_content: Any,
        assistant_content: Any,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Sync a completed turn to all providers.

        Runs on a background worker thread, NOT inline on the
        turn-completion path. A provider's ``sync_turn`` may make a
        blocking network/daemon call (a misconfigured Hindsight daemon
        was observed blocking ~298s before failing); doing that inline
        held ``run_conversation`` open long after the user saw their
        response, so every interface (CLI, TUI, gateway) kept the agent
        marked "running" for minutes and any follow-up message triggered
        an aggressive interrupt. Dispatching off-thread means a slow or
        broken provider can never stall the turn — the sync simply
        completes (or fails, logged) in the background.

        Writes are serialized through a single worker so turn N lands
        before turn N+1; provider implementations don't need their own
        ordering guarantees.
        """
        if self.read_only:
            return
        providers = list(self._providers)
        if not providers:
            return

        normalized_user_content = normalize_memory_turn_content(user_content)
        normalized_assistant_content = normalize_memory_turn_content(assistant_content)
        if not _has_recoverable_memory_text(normalized_user_content):
            return
        clean_user_content = self._strip_skill_scaffolding(normalized_user_content)
        if not clean_user_content:
            return
        user_content = clean_user_content
        assistant_content = normalized_assistant_content

        def _run() -> None:
            for provider in providers:
                try:
                    if messages is not None and self._provider_sync_accepts_messages(provider):
                        provider.sync_turn(
                            user_content,
                            assistant_content,
                            session_id=session_id,
                            messages=messages,
                        )
                    else:
                        provider.sync_turn(
                            user_content,
                            assistant_content,
                            session_id=session_id,
                        )
                except Exception as e:
                    logger.warning(
                        "Memory provider '%s' sync_turn failed: %s",
                        provider.name, e,
                    )

        self._submit_background(_run)

    # -- Background dispatch -------------------------------------------------

    def _submit_background(self, fn, *, kind: str = "write") -> None:
        """Queue ``fn`` on the serialized worker and track its durability class."""
        executor = self._get_sync_executor()
        if executor is None:
            if self._shutting_down:
                logger.warning("Memory manager is shutting down; rejecting late %s task", kind)
                return
            # Creation failure outside shutdown: preserve the historical
            # fail-safe behavior and run the operation inline.
            try:
                fn()
            except Exception as e:  # pragma: no cover - fn guards internally
                logger.debug("Inline memory background task failed: %s", e)
            return
        try:
            # Make submit+tracking atomic with the shutdown snapshot. The
            # callback is attached after releasing the lock because an already
            # completed future invokes callbacks synchronously.
            with self._sync_executor_lock:
                if self._shutting_down:
                    logger.warning("Memory manager is shutting down; rejecting late %s task", kind)
                    return
                future = executor.submit(fn)
                self._background_futures[future] = kind
            future.add_done_callback(self._forget_background_future)
        except RuntimeError:
            if self._shutting_down:
                logger.warning("Memory manager shut down during %s submission; task rejected", kind)
                return
            try:
                fn()
            except Exception as e:  # pragma: no cover - fn guards internally
                logger.debug("Inline memory background task failed: %s", e)

    def _forget_background_future(self, future: Future) -> None:
        with self._sync_executor_lock:
            self._background_futures.pop(future, None)

    def _get_sync_executor(self) -> Optional[ThreadPoolExecutor]:
        """Lazily create the single-worker background executor."""
        if self._shutting_down:
            return None
        if self._sync_executor is not None:
            return self._sync_executor
        with self._sync_executor_lock:
            if self._shutting_down:
                return None
            if self._sync_executor is None:
                try:
                    # Daemon workers (see tools.daemon_pool): a provider wedged
                    # on a network call must never block interpreter exit.
                    from tools.daemon_pool import DaemonThreadPoolExecutor
                    self._sync_executor = DaemonThreadPoolExecutor(
                        max_workers=1,
                        thread_name_prefix="mem-sync",
                    )
                except Exception as e:  # pragma: no cover - resource exhaustion
                    logger.warning("Failed to create memory sync executor: %s", e)
                    return None
            return self._sync_executor

    def flush_pending(self, timeout: Optional[float] = None) -> bool:
        """Block until queued sync/prefetch work has drained.

        Single-worker executor means submitting a sentinel and waiting on
        it guarantees every previously-submitted task has run. Returns
        True if the barrier completed within ``timeout`` (or no executor
        exists), False on timeout. Used at real session boundaries and by
        tests that need to assert provider state deterministically.
        """
        executor = self._sync_executor
        if executor is None:
            return True
        try:
            fut = executor.submit(lambda: None)
        except RuntimeError:
            # Executor already shut down — nothing pending.
            return True
        try:
            fut.result(timeout=timeout)
            return True
        except Exception:
            return False

    # -- Tools ---------------------------------------------------------------

    def get_all_tool_schemas(self) -> List[Dict[str, Any]]:
        """Collect tool schemas from all providers."""
        schemas = []
        seen = set()
        for provider in self._providers:
            try:
                for raw_schema in provider.get_tool_schemas():
                    schema = normalize_tool_schema(raw_schema)
                    if schema is None:
                        continue
                    if self.read_only and self.is_write_like_tool_schema(schema):
                        continue
                    name = schema.get("name", "")
                    if self.is_reserved_tool_name(name):
                        continue
                    if name and name not in seen:
                        schemas.append(schema)
                        seen.add(name)
            except Exception as e:
                logger.warning(
                    "Memory provider '%s' get_tool_schemas() failed: %s",
                    provider.name, e,
                )
        return schemas

    def get_all_tool_names(self) -> set:
        """Return set of all tool names across all providers."""
        return set(self._tool_to_provider.keys())

    def has_tool(self, tool_name: str) -> bool:
        """Check if any provider handles this tool."""
        return tool_name in self._tool_to_provider

    def handle_tool_call(
        self, tool_name: str, args: Dict[str, Any], **kwargs
    ) -> str:
        """Route a tool call to the correct provider.

        Returns JSON string result. Raises ValueError if no provider
        handles the tool.
        """
        provider = self._tool_to_provider.get(tool_name)
        if provider is None:
            if self.read_only:
                if _WRITE_TOOL_RE.search(tool_name):
                    return tool_error(
                        f"Memory tool '{tool_name}' is blocked in read-only memory mode"
                    )
                return tool_error(
                    f"Memory tool '{tool_name}' is not available in read-only memory mode"
                )
            return tool_error(f"No memory provider handles tool '{tool_name}'")
        if self.read_only:
            try:
                for schema in provider.get_tool_schemas():
                    if schema.get("name") == tool_name and self.is_write_like_tool_schema(schema):
                        return tool_error(
                            f"Memory tool '{tool_name}' is blocked in read-only memory mode"
                        )
            except Exception:
                if _WRITE_TOOL_RE.search(tool_name):
                    return tool_error(
                        f"Memory tool '{tool_name}' is blocked in read-only memory mode"
                    )
        try:
            return provider.handle_tool_call(tool_name, args, **kwargs)
        except Exception as e:
            logger.error(
                "Memory provider '%s' handle_tool_call(%s) failed: %s",
                provider.name, tool_name, e,
            )
            return tool_error(f"Memory tool '{tool_name}' failed: {e}")

    # -- Lifecycle hooks -----------------------------------------------------

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        """Notify all providers of a new turn.

        kwargs may include: remaining_tokens, model, platform, tool_count.
        """
        for provider in self._providers:
            try:
                provider.on_turn_start(turn_number, message, **kwargs)
            except Exception as e:
                logger.debug(
                    "Memory provider '%s' on_turn_start failed: %s",
                    provider.name, e,
                )

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        """Notify all providers of session end."""
        if self.read_only:
            return
        for provider in self._providers:
            try:
                provider.on_session_end(messages)
            except Exception as e:
                logger.debug(
                    "Memory provider '%s' on_session_end failed: %s",
                    provider.name, e,
                )

    def commit_session_boundary_async(
        self,
        messages: List[Dict[str, Any]],
        *,
        new_session_id: str,
        parent_session_id: str = "",
        reason: str = "new_session",
    ) -> None:
        """Queue old-session extraction + provider rebinding as ONE serialized task.

        Session rotation (/new) must deliver ``on_session_end`` (end-of-session
        extraction — an LLM-bound call that can take seconds) strictly BEFORE
        ``on_session_switch`` (which rebinds provider-internal ``_session_id`` /
        turn buffers to the new session). Running extraction inline blocked the
        /new command for the whole LLM round-trip (#16454); running it on an
        ad-hoc thread raced the inline switch — providers key off internal
        state, so a late ``on_session_end`` ran against post-switch bindings
        (transcript misattributed to the new session id, double-ingest of the
        old turn buffer, new-session buffers cleared).

        Submitting BOTH hooks as one task on the manager's single background
        worker gives both properties at a single chokepoint: the caller returns
        immediately, and the worker's FIFO order serializes end→switch against
        every other provider write (per-turn ``sync_all``, prefetches), which
        already share the same worker. If the executor is unavailable,
        ``_submit_background`` degrades to inline execution — the pre-#16454
        synchronous behavior, slow but correct.
        """
        if not self._providers:
            return
        snapshot = list(messages or [])

        def _run() -> None:
            try:
                self.on_session_end(snapshot)
            except Exception as e:  # pragma: no cover - on_session_end guards per-provider
                logger.warning("Session-boundary extraction failed: %s", e)
            try:
                self.on_session_switch(
                    new_session_id,
                    parent_session_id=parent_session_id,
                    reset=True,
                    reason=reason,
                )
            except Exception as e:  # pragma: no cover - on_session_switch guards per-provider
                logger.warning("Session-boundary switch failed: %s", e)

        self._submit_background(_run)

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        **kwargs,
    ) -> None:
        """Notify all providers that the agent's session_id has rotated.

        Fires on ``/resume``, ``/branch``, ``/reset``, ``/new``, and
        context compression — any path that reassigns
        ``AIAgent.session_id`` without tearing the provider down.

        Providers keep running; they only need to refresh cached
        per-session state so subsequent writes land in the correct
        session's record. See ``MemoryProvider.on_session_switch`` for
        the full contract.
        """
        if not new_session_id:
            return
        for provider in self._providers:
            try:
                provider.on_session_switch(
                    new_session_id,
                    parent_session_id=parent_session_id,
                    reset=reset,
                    **kwargs,
                )
            except Exception as e:
                logger.debug(
                    "Memory provider '%s' on_session_switch failed: %s",
                    provider.name, e,
                )

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        """Notify all providers before context compression.

        Returns combined text from providers to include in the compression
        summary prompt. Empty string if no provider contributes.
        """
        if self.read_only:
            return ""
        parts = []
        for provider in self._providers:
            try:
                result = provider.on_pre_compress(messages)
                if result and result.strip():
                    parts.append(result)
            except Exception as e:
                logger.debug(
                    "Memory provider '%s' on_pre_compress failed: %s",
                    provider.name, e,
                )
        return "\n\n".join(parts)

    @staticmethod
    def _provider_memory_write_metadata_mode(provider: MemoryProvider) -> str:
        """Return how to pass metadata to a provider's memory-write hook."""
        try:
            signature = inspect.signature(provider.on_memory_write)
        except (TypeError, ValueError):
            return "keyword"

        params = list(signature.parameters.values())
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params):
            return "keyword"
        if "metadata" in signature.parameters:
            return "keyword"

        accepted = [
            p for p in params
            if p.kind in {
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            }
        ]
        if len(accepted) >= 4:
            return "positional"
        return "legacy"

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Notify external providers when the built-in memory tool writes.

        Skips the builtin provider itself (it's the source of the write).
        """
        if self.read_only:
            return
        for provider in self._providers:
            if provider.name == "builtin":
                continue
            try:
                metadata_mode = self._provider_memory_write_metadata_mode(provider)
                if metadata_mode == "keyword":
                    provider.on_memory_write(
                        action, target, content, metadata=dict(metadata or {})
                    )
                elif metadata_mode == "positional":
                    provider.on_memory_write(action, target, content, dict(metadata or {}))
                else:
                    provider.on_memory_write(action, target, content)
            except Exception as e:
                logger.debug(
                    "Memory provider '%s' on_memory_write failed: %s",
                    provider.name, e,
                )

    _MIRRORED_MEMORY_ACTIONS = {"add", "replace", "remove"}

    @staticmethod
    def _memory_tool_result_succeeded(result: Any) -> bool:
        """Return whether the built-in memory tool committed a write."""
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except Exception:
                return False
        if not isinstance(result, dict):
            return False
        return result.get("success") is True and result.get("staged") is not True

    def notify_memory_tool_write(
        self,
        tool_result: Any,
        tool_args: Dict[str, Any],
        *,
        build_metadata: Optional[Callable[[], Dict[str, Any]]] = None,
    ) -> None:
        """Mirror a committed built-in memory write to external providers."""
        if self.read_only or not self._memory_tool_result_succeeded(tool_result):
            return

        target = str(tool_args.get("target") or "memory")
        operations = tool_args.get("operations")
        if isinstance(operations, list) and operations:
            raw_operations = operations
        else:
            raw_operations = [{
                "action": tool_args.get("action"),
                "content": tool_args.get("content"),
                "old_text": tool_args.get("old_text"),
            }]

        for op in raw_operations:
            if not isinstance(op, dict):
                continue
            action = str(op.get("action") or "")
            if action not in self._MIRRORED_MEMORY_ACTIONS:
                continue
            try:
                metadata = dict(build_metadata() if build_metadata else {})
                old_text = op.get("old_text")
                if old_text:
                    metadata["old_text"] = str(old_text)
                self.on_memory_write(
                    action,
                    target,
                    str(op.get("content") or ""),
                    metadata=metadata,
                )
            except Exception as e:
                logger.debug("notify_memory_tool_write failed for op %s: %s", action, e)

    def on_delegation(self, task: str, result: str, *,
                      child_session_id: str = "", **kwargs) -> None:
        """Notify all providers that a subagent completed."""
        if self.read_only:
            return
        for provider in self._providers:
            try:
                provider.on_delegation(
                    task, result, child_session_id=child_session_id, **kwargs
                )
            except Exception as e:
                logger.debug(
                    "Memory provider '%s' on_delegation failed: %s",
                    provider.name, e,
                )

    def shutdown_all(self) -> None:
        """Shut down all providers (reverse order for clean teardown).

        Drains the background sync/prefetch executor first (bounded by
        ``_SYNC_DRAIN_TIMEOUT_S``) so a turn's final sync has a chance to
        land before providers are torn down. The worker threads are
        daemon, so anything still wedged past the drain window dies with
        the interpreter rather than blocking exit.
        """
        self._drain_sync_executor()
        for provider in reversed(self._providers):
            try:
                provider.shutdown()
            except Exception as e:
                logger.warning(
                    "Memory provider '%s' shutdown failed: %s",
                    provider.name, e,
                )


    @property
    def shutdown_drain_state(self) -> Dict[str, Any]:
        """Snapshot of the most recent bounded shutdown drain outcome."""
        with self._sync_executor_lock:
            return dict(self._shutdown_drain_state)

    def _drain_sync_executor(self) -> None:
        """Give queued FIFO work a bounded chance, then abandon explicitly."""
        with self._sync_executor_lock:
            self._shutting_down = True
            executor = self._sync_executor
            self._sync_executor = None
            tracked = dict(self._background_futures)
            self._shutdown_drain_state = {
                "status": "draining" if executor is not None else "drained",
                "abandoned_writes": 0,
                "abandoned_prefetches": 0,
                "active_tasks": sum(not future.done() for future in tracked),
            }
        if executor is None:
            return

        # shutdown(wait=False) closes submission without touching the FIFO.
        # Waiting on the tracked futures lets the real single-worker executor
        # run every queued write/boundary task in order up to the deadline.
        executor.shutdown(wait=False, cancel_futures=False)
        _, pending = wait(tuple(tracked), timeout=_SYNC_DRAIN_TIMEOUT_S)
        if not pending:
            with self._sync_executor_lock:
                self._shutdown_drain_state.update(status="drained", active_tasks=0)
            return

        abandoned_writes = 0
        abandoned_prefetches = 0
        active_tasks = 0
        for future in pending:
            kind = tracked[future]
            if future.cancel():
                if kind == "prefetch":
                    abandoned_prefetches += 1
                else:
                    abandoned_writes += 1
            else:
                active_tasks += 1

        with self._sync_executor_lock:
            self._shutdown_drain_state.update(
                status="timed_out",
                abandoned_writes=abandoned_writes,
                abandoned_prefetches=abandoned_prefetches,
                active_tasks=active_tasks,
            )
        logger.warning(
            "Memory shutdown drain timed out after %.2fs; abandoning %d queued "
            "memory write(s) and %d queued prefetch(es); %d active task(s) remain detached",
            _SYNC_DRAIN_TIMEOUT_S,
            abandoned_writes,
            abandoned_prefetches,
            active_tasks,
        )

    def initialize_all(self, session_id: str, **kwargs) -> None:
        """Initialize all providers.

        Automatically injects ``hermes_home`` into *kwargs* so that every
        provider can resolve profile-scoped storage paths without importing
        ``get_hermes_home()`` themselves.
        """
        if "hermes_home" not in kwargs:
            from hermes_constants import get_hermes_home
            kwargs["hermes_home"] = str(get_hermes_home())
        for provider in self._providers:
            try:
                provider.initialize(session_id=session_id, **kwargs)
            except Exception as e:
                logger.warning(
                    "Memory provider '%s' initialize failed: %s",
                    provider.name, e,
                )
