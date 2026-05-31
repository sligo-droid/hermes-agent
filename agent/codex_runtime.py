"""Codex API runtime — App Server and Responses-API streaming paths.

Extracted from :class:`AIAgent` to keep the agent loop file focused.
Each function takes the parent ``AIAgent`` as its first argument
(``agent``).  AIAgent keeps thin forwarder methods for backward
compatibility.

* ``run_codex_app_server_turn`` — drives one turn through the
  ``codex_app_server`` subprocess client (used when a Codex CLI install
  is the active provider).
* ``run_codex_stream`` — streams a Codex Responses API call (the
  ``codex_responses`` api_mode).
* ``run_codex_create_stream_fallback`` — recovery path when the
  Responses ``stream=True`` initial create fails.
"""

from __future__ import annotations

import logging
import os
import time
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _resolved_codex_app_server_turn_timeout() -> float:
    """Resolve the per-turn Codex app-server deadline in seconds."""
    try:
        from hermes_cli.config import load_config

        agent_cfg = (load_config() or {}).get("agent") or {}
        timeout = float(agent_cfg.get("codex_app_server_turn_timeout") or 1800.0)
        if timeout > 0:
            return timeout
    except Exception:
        pass
    return 1800.0


def _codex_timeout_continuation_prompt(
    *,
    original_user_message: Any,
    timeout_seconds: float,
    attempt: int,
    max_attempts: int,
    error: Optional[str],
) -> str:
    from agent.codex_responses_adapter import _summarize_user_message_for_log

    if isinstance(original_user_message, str):
        original = original_user_message
    else:
        original = _summarize_user_message_for_log(original_user_message)
    detail = f"\nLast timeout: {error}" if error else ""
    return (
        "[Hermes internal continuation after Codex app-server timeout]\n"
        f"The previous Codex app-server turn timed out after {timeout_seconds:.0f}s "
        f"({attempt}/{max_attempts}). This timeout is not a signal that the "
        f"overall effort should stop.{detail}\n\n"
        "Continue the same task from the next concrete step. If useful, "
        "briefly account for any completed work before proceeding.\n\n"
        f"Original user request:\n{original}"
    )


def _close_codex_app_server_session(agent) -> None:
    session = getattr(agent, "_codex_session", None)
    if session is not None:
        try:
            session.close()
        except Exception:
            pass
        try:
            from agent.codex_worker_auth import sync_codex_worker_home

            sync_codex_worker_home(
                getattr(agent, "_codex_worker_home", None),
                getattr(agent, "_codex_worker_credential_id", None),
            )
        except Exception:
            pass
        lease = getattr(agent, "_codex_worker_home_lease", None)
        if lease is not None:
            try:
                lease.cleanup()
            except Exception:
                pass
    agent._codex_session = None
    agent._codex_worker_home = None
    agent._codex_worker_credential_id = None
    agent._codex_worker_home_lease = None


def _record_codex_app_server_runtime(agent, duration: float) -> None:
    stats = getattr(agent, "_turn_runtime_stats", None)
    if not isinstance(stats, dict):
        return
    try:
        stats["api_calls"] = int(stats.get("api_calls") or 0) + 1
        stats["api_duration_s"] = float(stats.get("api_duration_s") or 0.0) + max(
            0.0, float(duration or 0.0)
        )
    except Exception:
        logger.debug("codex app-server runtime accounting failed", exc_info=True)


def _codex_kanban_worker_bootstrap() -> str:
    """Return concise Codex-only guidance for kanban worker sessions."""
    if not os.environ.get("HERMES_KANBAN_TASK"):
        return ""

    software_skills: list[str] = []
    general_available = False
    try:
        from tools.skills_tool import _find_all_skills

        for entry in _find_all_skills():
            name = str(entry.get("name") or "").strip()
            category = str(entry.get("category") or "").strip()
            if name == "general-coding":
                general_available = True
            if category == "software-development" and name:
                software_skills.append(name)
    except Exception:
        software_skills = []

    skill_lines = []
    if general_available:
        skill_lines.append(
            "- `general-coding` (`~/AGENTS.md`): load before coding, repo "
            "edits, debugging, or tests."
        )
    if software_skills:
        listed = ", ".join(sorted(dict.fromkeys(software_skills)))
        skill_lines.append(
            f"- `software-development/*` skills available via `skill_view`: "
            f"{listed}."
        )
    else:
        skill_lines.append(
            "- Use `skills_list` to discover available "
            "`software-development/*` skills."
        )

    return (
        "[Hermes kanban Codex worker bootstrap]\n"
        "You are a Hermes kanban worker running through Codex app-server. "
        "Codex has shell/apply-patch for file work, and the Hermes MCP callback "
        "exposes `kanban_*`, `skill_view`, and `skills_list`.\n\n"
        "Required startup:\n"
        "1. Call `kanban_show()` first and treat its `worker_context` as the task truth.\n"
        "2. For ordinary coding, repo editing, debugging, or tests, call "
        "`skill_view(name=\"general-coding\")` before implementing. This is the "
        "same content as `~/AGENTS.md`.\n"
        "3. Load only the specific `software-development/*` skills that match "
        "the task, for example debugging, test-driven development, planning, "
        "or code review. Do not assume these skills are preloaded.\n\n"
        "Accessible coding skills:\n"
        + "\n".join(skill_lines)
        + "\n\n"
        "Original user request follows.\n"
        "[/Hermes kanban Codex worker bootstrap]\n\n"
    )


def run_codex_app_server_turn(
    agent,
    *,
    user_message: str,
    original_user_message: Any,
    messages: List[Dict[str, Any]],
    effective_task_id: str,
    conversation_history: Optional[List[Dict[str, Any]]] = None,
    should_review_memory: bool = False,
) -> Dict[str, Any]:
    """Codex app-server runtime path. Hands the entire turn to a `codex
    app-server` subprocess and projects its events back into Hermes'
    messages list so memory/skill review keep working.

    Called from run_conversation() when agent.api_mode == "codex_app_server".
    Returns the same dict shape as the chat_completions path.
    """
    from agent.transports.codex_app_server_session import CodexAppServerSession

    def _ensure_codex_session() -> None:
        if hasattr(agent, "_codex_session") and agent._codex_session is not None:
            return
        cwd = getattr(agent, "session_cwd", None) or os.getcwd()
        # Approval callback: defer to Hermes' standard prompt flow if a
        # CLI thread has installed one. Gateway / cron contexts get the
        # codex-side fail-closed default.
        try:
            from tools.terminal_tool import _get_approval_callback
            approval_callback = _get_approval_callback()
        except Exception:
            approval_callback = None

        def _codex_event_activity(note: dict) -> None:
            method = note.get("method", "")
            item = ((note.get("params") or {}).get("item") or {})
            item_type = item.get("type") or ""
            suffix = f": {item_type}" if item_type else ""
            agent._touch_activity(f"Codex app-server event: {method}{suffix}")

        codex_home = getattr(agent, "_codex_worker_home", None)
        if codex_home is None:
            try:
                from agent.codex_worker_auth import create_codex_worker_home

                lease = create_codex_worker_home(
                    parent_agent=agent,
                    prefix=f"session-{os.getpid()}-",
                )
                codex_home = str(lease.path)
                agent._codex_worker_home = codex_home
                agent._codex_worker_credential_id = lease.credential_id
                agent._codex_worker_home_lease = lease
            except Exception:
                codex_home = None

        agent._codex_session = CodexAppServerSession(
            cwd=cwd,
            codex_home=codex_home,
            approval_callback=approval_callback,
            on_event=_codex_event_activity,
        )

    # NOTE: the user message is ALREADY appended to messages by the
    # standard run_conversation() flow (line ~11823) before the early
    # return reaches us. Do NOT append again — that would duplicate.
    codex_input = user_message
    if not getattr(agent, "_codex_kanban_bootstrap_sent", False):
        bootstrap = _codex_kanban_worker_bootstrap()
        if bootstrap and isinstance(codex_input, str):
            codex_input = bootstrap + codex_input
            agent._codex_kanban_bootstrap_sent = True
    turn_timeout = _resolved_codex_app_server_turn_timeout()
    api_call_count = 0
    last_timeout_error: Optional[str] = None
    turn = None

    from agent.codex_responses_adapter import _summarize_user_message_for_log

    while api_call_count < agent.max_iterations and agent.iteration_budget.remaining > 0:
        if agent._interrupt_requested:
            return {
                "final_response": "",
                "messages": messages,
                "api_calls": api_call_count,
                "completed": False,
                "partial": True,
                "interrupted": True,
                "interrupt_message": agent._interrupt_message,
                "error": agent._interrupt_message or "interrupted",
            }

        api_call_count += 1
        agent._api_call_count = api_call_count
        agent._touch_activity(f"running Codex app-server turn #{api_call_count}")
        if not agent.iteration_budget.consume():
            break

        _ensure_codex_session()

        codex_api_start = time.perf_counter()
        try:
            turn = agent._codex_session.run_turn(
                user_input=codex_input,
                turn_timeout=turn_timeout,
            )
            _record_codex_app_server_runtime(
                agent, time.perf_counter() - codex_api_start
            )
        except Exception as exc:
            _record_codex_app_server_runtime(
                agent, time.perf_counter() - codex_api_start
            )
            logger.exception("codex app-server turn failed")
            # Crash -> unconditionally drop the session so the next turn
            # respawns from scratch instead of reusing a dead client.
            _close_codex_app_server_session(agent)
            final_response = (
                f"Codex app-server turn failed: {exc}. "
                f"Fall back to default runtime with `/codex-runtime auto`."
            )
            messages.append({"role": "assistant", "content": final_response})
            agent._save_trajectory(
                messages,
                _summarize_user_message_for_log(user_message),
                completed=False,
            )
            agent._cleanup_task_resources(effective_task_id)
            agent._persist_session(messages, conversation_history)
            return {
                "final_response": final_response,
                "messages": messages,
                "api_calls": api_call_count,
                "completed": False,
                "partial": True,
                "error": str(exc),
            }
        try:
            from agent.codex_worker_auth import sync_codex_worker_home

            sync_codex_worker_home(
                getattr(agent, "_codex_worker_home", None),
                getattr(agent, "_codex_worker_credential_id", None),
            )
        except Exception:
            pass

        # Splice projected messages into the conversation. The projector emits
        # standard {role, content, tool_calls, tool_call_id} entries, which
        # is exactly what curator.py / sessions DB expect. Codex also echoes
        # userMessage events; drop those because run_conversation already
        # appended the canonical user turn before entering this path.
        if turn.projected_messages:
            messages.extend(
                msg for msg in turn.projected_messages
                if msg.get("role") != "user"
            )

        # Counter ticks for the self-improvement loop. _turns_since_memory and
        # _user_turn_count are already incremented in run_conversation().
        agent._iters_since_skill = (
            getattr(agent, "_iters_since_skill", 0) + turn.tool_iterations
        )

        # Retire a wedged subprocess before any timeout continuation starts a
        # fresh Codex turn.
        if getattr(turn, "should_retire", False):
            logger.warning(
                "codex app-server session retired (turn error: %s)",
                turn.error,
            )
            _close_codex_app_server_session(agent)

        if getattr(turn, "timed_out", False):
            last_timeout_error = turn.error or "Codex app-server turn timed out"
            logger.warning(
                "codex app-server turn timed out after %.0fs; continuing "
                "within Hermes turn budget (%d/%d)",
                turn_timeout, api_call_count, agent.max_iterations,
            )
            if (
                api_call_count >= agent.max_iterations
                or agent.iteration_budget.remaining <= 0
            ):
                break
            codex_input = _codex_timeout_continuation_prompt(
                original_user_message=original_user_message,
                timeout_seconds=turn_timeout,
                attempt=api_call_count,
                max_attempts=agent.max_iterations,
                error=last_timeout_error,
            )
            continue

        break

    if turn is None or getattr(turn, "timed_out", False):
        final_response = (
            "Codex app-server timed out and Hermes reached the configured "
            f"turn budget ({api_call_count}/{agent.max_iterations})."
        )
        if last_timeout_error:
            final_response = f"{final_response} Last timeout: {last_timeout_error}"
        messages.append({"role": "assistant", "content": final_response})
        agent._save_trajectory(
            messages,
            _summarize_user_message_for_log(user_message),
            completed=False,
        )
        agent._cleanup_task_resources(effective_task_id)
        agent._persist_session(messages, conversation_history)
        return {
            "final_response": final_response,
            "messages": messages,
            "api_calls": api_call_count,
            "completed": False,
            "partial": True,
            "error": last_timeout_error or "Codex app-server turn timed out",
        }

    final_response = turn.final_text
    if turn.error and not final_response:
        final_response = f"Codex app-server turn failed: {turn.error}"

    if (
        final_response
        and not (
            messages
            and messages[-1].get("role") == "assistant"
            and messages[-1].get("content") == final_response
        )
    ):
        messages.append({"role": "assistant", "content": final_response})

    # Now check the skill nudge AFTER iters were incremented — same
    # pattern the chat_completions path uses (line ~15432).
    should_review_skills = False
    if (
        agent._skill_nudge_interval > 0
        and agent._iters_since_skill >= agent._skill_nudge_interval
        and "skill_manage" in agent.valid_tool_names
    ):
        should_review_skills = True
        agent._iters_since_skill = 0

    # External memory provider sync (mirrors line ~15439). Skipped on
    # interrupt/error to avoid feeding partial transcripts to memory.
    if not turn.interrupted and turn.error is None:
        try:
            agent._sync_external_memory_for_turn(
                original_user_message=original_user_message,
                final_response=turn.final_text,
                interrupted=False,
            )
        except Exception:
            logger.debug("external memory sync raised", exc_info=True)

    # Background review fork — same cadence + signature as the default
    # path (line ~15449). Only fires when a trigger actually tripped AND
    # we have a real final response.
    if (
        final_response
        and not turn.interrupted
        and turn.error is None
        and (should_review_memory or should_review_skills)
    ):
        try:
            agent._spawn_background_review(
                messages_snapshot=list(messages),
                review_memory=should_review_memory,
                review_skills=should_review_skills,
            )
        except Exception:
            logger.debug("background review spawn raised", exc_info=True)

    completed = not turn.interrupted and turn.error is None
    agent._save_trajectory(
        messages,
        _summarize_user_message_for_log(user_message),
        completed=completed,
    )
    agent._cleanup_task_resources(effective_task_id)
    agent._persist_session(messages, conversation_history)

    return {
        "final_response": final_response,
        "messages": messages,
        "api_calls": api_call_count,
        "completed": completed,
        "partial": turn.interrupted or turn.error is not None,
        "error": turn.error,
        "codex_thread_id": turn.thread_id,
        "codex_turn_id": turn.turn_id,
    }


# ---------------------------------------------------------------------------
# Event-driven Responses streaming
#
# OpenAI ships its consumer Codex backend (chatgpt.com/backend-api/codex) on
# a different schedule from the openai Python SDK.  The high-level
# ``client.responses.stream(...)`` helper reconstructs a typed Response from
# the terminal ``response.completed`` event's ``response.output`` field, and
# when that field drifts to ``null`` (gpt-5.5, May 2026) the SDK raises
# ``TypeError: 'NoneType' object is not iterable`` mid-iteration.
#
# We sidestep the whole class of failure by going one level lower:
# ``client.responses.create(stream=True)`` returns the raw AsyncIterable of
# SSE events, and we assemble the final response object purely from
# ``response.output_item.done`` events as they arrive.  We never read
# ``response.completed.response.output`` for content reconstruction, so the
# backend can return ``null``, ``[]``, a string, or omit the field entirely
# and we don't care.
#
# This mirrors what the OpenClaw TS implementation does for the same backend
# and is structurally immune to the bug class rather than patched.
# ---------------------------------------------------------------------------


_TERMINAL_EVENT_TYPES = frozenset({
    "response.completed",
    "response.incomplete",
    "response.failed",
})


def _event_field(event: Any, name: str, default: Any = None) -> Any:
    """Field access that handles both attr-style (SDK objects) and dict (raw JSON) events."""
    value = getattr(event, name, None)
    if value is None and isinstance(event, dict):
        value = event.get(name, default)
    return value if value is not None else default


def _raise_stream_error(event: Any) -> None:
    """Raise a ``_StreamErrorEvent`` from a ``type=error`` SSE frame.

    Imported lazily so this module stays importable from places that don't
    pull in ``run_agent`` (e.g. plugin code, doc tools).
    """
    from run_agent import _StreamErrorEvent
    message = (_event_field(event, "message", "") or "stream emitted error event").strip()
    raise _StreamErrorEvent(
        message,
        code=_event_field(event, "code"),
        param=_event_field(event, "param"),
    )


def _consume_codex_event_stream(
    event_iter: Any,
    *,
    model: str,
    on_text_delta=None,
    on_reasoning_delta=None,
    on_first_delta=None,
    on_event=None,
    interrupt_check=None,
) -> SimpleNamespace:
    """Consume a Codex Responses SSE event stream and return a final response.

    The returned object is a ``SimpleNamespace`` shaped like the SDK's typed
    ``Response`` for the fields downstream code actually reads:

    * ``output``: list of output items, assembled from ``response.output_item.done``.
      For tool-call turns this contains the function_call items; for plain-text
      turns it contains a synthesized ``message`` item built from streamed deltas
      if no message item was emitted directly.
    * ``output_text``: assembled text from ``response.output_text.delta`` deltas.
    * ``usage``: copied from the terminal event's ``response.usage`` (when present).
    * ``status``: ``completed`` / ``incomplete`` / ``failed`` (or ``completed`` if
      the stream ended without a terminal frame but produced content).
    * ``id``: ``response.id`` when present.
    * ``incomplete_details``: passed through for ``response.incomplete`` frames.
    * ``error``: passed through for ``response.failed`` frames.
    * ``model``: from kwargs (the wire model name is not authoritative).

    Critically, we never read ``response.output`` from the terminal event for
    content reconstruction — only ``usage``, ``status``, ``id``.  That field
    being ``null`` / ``[]`` / missing is fine.

    Callbacks:

    * ``on_text_delta(str)`` — fires per ``response.output_text.delta``, suppressed
      once a function_call event is seen (so tool-call turns don't bleed text
      into the chat).
    * ``on_reasoning_delta(str)`` — fires per ``response.reasoning.*.delta``.
    * ``on_first_delta()`` — one-shot, fires on the first text delta only.
    * ``on_event(event)`` — fires for every event before any other processing.
      Used for watchdog activity, debug logging, anything wire-shape-agnostic.
    * ``interrupt_check()`` — returns True to break the loop early.
    """
    collected_output_items: List[Any] = []
    collected_text_deltas: List[str] = []
    has_tool_calls = False
    first_delta_fired = False
    terminal_status: str = "completed"
    terminal_usage: Any = None
    terminal_response_id: str = None
    terminal_incomplete_details: Any = None
    terminal_error: Any = None
    saw_terminal = False

    for event in event_iter:
        if on_event is not None:
            try:
                on_event(event)
            except (TimeoutError, InterruptedError):
                # Control-flow signals from watchdog/cancellation hooks must
                # propagate, not get swallowed as "debug noise".
                raise
            except Exception:
                # Genuine bugs in third-party debug/log hooks shouldn't break
                # stream consumption.
                logger.debug("Codex stream on_event hook raised", exc_info=True)
        if interrupt_check is not None and interrupt_check():
            break

        event_type = _event_field(event, "type", "")
        if not isinstance(event_type, str):
            event_type = ""

        # ``error`` SSE frames carry the provider's real failure reason
        # (subscription / quota / model-not-available / rejected-reasoning-replay)
        # but never appear in the terminal set.  Surface them as a structured
        # exception so the credential pool + error classifier see the body.
        if event_type == "error":
            _raise_stream_error(event)

        if "output_text.delta" in event_type or event_type == "response.output_text.delta":
            delta_text = _event_field(event, "delta", "")
            if delta_text:
                collected_text_deltas.append(delta_text)
                if not has_tool_calls:
                    if not first_delta_fired:
                        first_delta_fired = True
                        if on_first_delta is not None:
                            try:
                                on_first_delta()
                            except Exception:
                                logger.debug("Codex stream on_first_delta raised", exc_info=True)
                    if on_text_delta is not None:
                        try:
                            on_text_delta(delta_text)
                        except Exception:
                            logger.debug("Codex stream on_text_delta raised", exc_info=True)
            continue

        if "function_call" in event_type:
            has_tool_calls = True
            # fall through — function_call items still get added on output_item.done

        if "reasoning" in event_type and "delta" in event_type:
            reasoning_text = _event_field(event, "delta", "")
            if reasoning_text and on_reasoning_delta is not None:
                try:
                    on_reasoning_delta(reasoning_text)
                except Exception:
                    logger.debug("Codex stream on_reasoning_delta raised", exc_info=True)
            continue

        if event_type == "response.output_item.done":
            done_item = _event_field(event, "item")
            if done_item is not None:
                collected_output_items.append(done_item)
            continue

        if event_type in _TERMINAL_EVENT_TYPES:
            saw_terminal = True
            resp_obj = _event_field(event, "response")
            if resp_obj is not None:
                terminal_usage = getattr(resp_obj, "usage", None)
                if terminal_usage is None and isinstance(resp_obj, dict):
                    terminal_usage = resp_obj.get("usage")
                rid = getattr(resp_obj, "id", None)
                if rid is None and isinstance(resp_obj, dict):
                    rid = resp_obj.get("id")
                terminal_response_id = rid
                rstatus = getattr(resp_obj, "status", None)
                if rstatus is None and isinstance(resp_obj, dict):
                    rstatus = resp_obj.get("status")
                if isinstance(rstatus, str):
                    terminal_status = rstatus
                if event_type == "response.incomplete":
                    terminal_incomplete_details = getattr(resp_obj, "incomplete_details", None)
                    if terminal_incomplete_details is None and isinstance(resp_obj, dict):
                        terminal_incomplete_details = resp_obj.get("incomplete_details")
                if event_type == "response.failed":
                    terminal_error = getattr(resp_obj, "error", None)
                    if terminal_error is None and isinstance(resp_obj, dict):
                        terminal_error = resp_obj.get("error")
            if event_type == "response.completed":
                terminal_status = terminal_status or "completed"
            elif event_type == "response.incomplete":
                terminal_status = terminal_status or "incomplete"
            elif event_type == "response.failed":
                terminal_status = terminal_status or "failed"
            # Stop on terminal event.
            break

    # Build the final output list.  Prefer items observed via output_item.done;
    # if none arrived but we streamed plain text deltas (no tool calls), synthesize
    # a single message item so downstream normalization has something to work with.
    if collected_output_items:
        output = list(collected_output_items)
    elif collected_text_deltas and not has_tool_calls:
        assembled = "".join(collected_text_deltas)
        output = [SimpleNamespace(
            type="message",
            role="assistant",
            status="completed",
            content=[SimpleNamespace(type="output_text", text=assembled)],
        )]
    else:
        output = []

    # If the stream ended without any terminal event AND produced no usable
    # content (no items, no text deltas), surface that as a RuntimeError so
    # callers can distinguish "stream truncated mid-flight / provider rejected
    # the call" from "stream completed with empty body".  This preserves the
    # signal the SDK's high-level helper used to raise as
    # ``RuntimeError("Didn't receive a `response.completed` event.")``.
    if not saw_terminal and not output:
        raise RuntimeError(
            "Codex Responses stream did not emit a terminal response"
        )

    assembled_text = "".join(collected_text_deltas)

    final = SimpleNamespace(
        output=output,
        output_text=assembled_text,
        usage=terminal_usage,
        status=terminal_status,
        id=terminal_response_id,
        model=model,
        incomplete_details=terminal_incomplete_details,
        error=terminal_error,
    )
    return final


def run_codex_stream(agent, api_kwargs: dict, client: Any = None, on_first_delta=None):
    """Execute one streaming Responses API request and return the final response.

    Uses ``responses.create(stream=True)`` (low-level raw event iteration)
    rather than the high-level ``responses.stream(...)`` helper.  This makes
    us structurally immune to backend drift in the ``response.completed``
    payload shape — we never let the SDK reconstruct a typed object from
    the terminal event's ``output`` field.
    """
    import httpx as _httpx

    active_client = client or agent._ensure_primary_openai_client(reason="codex_stream_direct")
    max_stream_retries = 1
    # Accumulate streamed text so callers / compat shims can read it.
    agent._codex_streamed_text_parts: list = []

    def _on_text_delta(text: str) -> None:
        agent._codex_streamed_text_parts.append(text)
        agent._fire_stream_delta(text)

    def _on_reasoning_delta(text: str) -> None:
        agent._fire_reasoning_delta(text)

    def _on_event(event: Any) -> None:
        # TTFB watchdog and activity touch — runs once per SSE event.
        agent._codex_stream_last_event_ts = time.time()
        agent._touch_activity("receiving stream response")

    def _interrupt_check() -> bool:
        return bool(agent._interrupt_requested)

    for attempt in range(max_stream_retries + 1):
        if agent._interrupt_requested:
            raise InterruptedError("Agent interrupted before Codex stream retry")

        stream_kwargs = dict(api_kwargs)
        stream_kwargs["stream"] = True

        try:
            event_stream = active_client.responses.create(**stream_kwargs)
        except (_httpx.RemoteProtocolError, _httpx.ReadTimeout, _httpx.ConnectError, ConnectionError) as exc:
            if attempt < max_stream_retries:
                logger.debug(
                    "Codex Responses stream connect failed (attempt %s/%s); retrying. %s error=%s",
                    attempt + 1, max_stream_retries + 1,
                    agent._client_log_context(), exc,
                )
                continue
            raise

        try:
            # Compatibility: some mocks/providers return a concrete response
            # instead of an iterable.  Pass it straight through.
            if hasattr(event_stream, "output") and not hasattr(event_stream, "__iter__"):
                return event_stream

            try:
                final = _consume_codex_event_stream(
                    event_stream,
                    model=api_kwargs.get("model"),
                    on_text_delta=_on_text_delta,
                    on_reasoning_delta=_on_reasoning_delta,
                    on_first_delta=on_first_delta,
                    on_event=_on_event,
                    interrupt_check=_interrupt_check,
                )
            except (_httpx.RemoteProtocolError, _httpx.ReadTimeout, _httpx.ConnectError, ConnectionError) as exc:
                if attempt < max_stream_retries:
                    logger.debug(
                        "Codex Responses stream transport failed mid-iteration "
                        "(attempt %s/%s); retrying. %s error=%s",
                        attempt + 1, max_stream_retries + 1,
                        agent._client_log_context(), exc,
                    )
                    continue
                raise

            if final.status in {"incomplete", "failed"}:
                logger.warning(
                    "Codex Responses stream terminal status=%s "
                    "(incomplete_details=%s, error=%s, streamed_chars=%d). %s",
                    final.status, final.incomplete_details, final.error,
                    sum(len(p) for p in agent._codex_streamed_text_parts),
                    agent._client_log_context(),
                )

            return final
        finally:
            close_fn = getattr(event_stream, "close", None)
            if callable(close_fn):
                try:
                    close_fn()
                except Exception:
                    pass


def run_codex_create_stream_fallback(agent, api_kwargs: dict, client: Any = None):
    """Backward-compatible alias for the unified event-driven path.

    Historically this was the fallback when the SDK's high-level
    ``responses.stream(...)`` helper raised on shape drift.  The primary
    path now does exactly what the fallback did, so this just forwards.
    Kept as a public symbol because tests and a small number of call sites
    still reference it by name.
    """
    return run_codex_stream(agent, api_kwargs, client=client)


__all__ = [
    "run_codex_app_server_turn",
    "run_codex_stream",
    "run_codex_create_stream_fallback",
    "_consume_codex_event_stream",
]
