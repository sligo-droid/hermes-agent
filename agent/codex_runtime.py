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

import json
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




def _is_null_output_stream_error(exc: TypeError) -> bool:
    message = str(exc)
    return "NoneType" in message and "not iterable" in message


def _synthesize_codex_stream_response(
    collected_output_items: list,
    collected_text_deltas: list,
    *,
    has_tool_calls: bool,
):
    if collected_output_items:
        output = list(collected_output_items)
        text = "".join(str(part) for part in collected_text_deltas if part)
    elif collected_text_deltas and not has_tool_calls:
        text = "".join(str(part) for part in collected_text_deltas if part)
        output = [SimpleNamespace(
            type="message",
            role="assistant",
            status="completed",
            content=[SimpleNamespace(type="output_text", text=text)],
        )]
    else:
        return None

    return SimpleNamespace(status="completed", output=output, output_text=text)


def _backfill_codex_stream_output(
    response,
    collected_output_items: list,
    collected_text_deltas: list,
    *,
    has_tool_calls: bool,
    log_label: str,
) -> bool:
    output = getattr(response, "output", None)
    if isinstance(output, list) and output:
        return False

    synthesized = _synthesize_codex_stream_response(
        collected_output_items,
        collected_text_deltas,
        has_tool_calls=has_tool_calls,
    )
    if synthesized is None:
        return False

    response.output = synthesized.output
    if not getattr(response, "output_text", None):
        response.output_text = synthesized.output_text
    logger.debug(
        "%s: backfilled %d output items from stream events (%d text chars)",
        log_label,
        len(synthesized.output),
        len(synthesized.output_text),
    )
    return True


def run_codex_stream(agent, api_kwargs: dict, client: Any = None, on_first_delta: callable = None):
    """Execute one streaming Responses API request and return the final response."""
    import httpx as _httpx

    active_client = client or agent._ensure_primary_openai_client(reason="codex_stream_direct")
    max_stream_retries = 1
    has_tool_calls = False
    first_delta_fired = False
    # Accumulate streamed text so we can recover if get_final_response()
    # returns empty output (e.g. chatgpt.com backend-api sends
    # response.incomplete instead of response.completed).
    agent._codex_streamed_text_parts: list = []
    for attempt in range(max_stream_retries + 1):
        if agent._interrupt_requested:
            raise InterruptedError("Agent interrupted before Codex stream retry")
        collected_output_items: list = []
        try:
            with active_client.responses.stream(**api_kwargs) as stream:
                for event in stream:
                    # Mark stream activity for the TTFB watchdog in
                    # interruptible_api_call. The Codex backend can accept the
                    # connection but never emit a single event; this timestamp
                    # staying None tells the watchdog no bytes are flowing.
                    agent._codex_stream_last_event_ts = time.time()
                    agent._touch_activity("receiving stream response")
                    if agent._interrupt_requested:
                        break
                    event_type = getattr(event, "type", "")
                    # Fire callbacks on text content deltas (suppress during tool calls)
                    if "output_text.delta" in event_type or event_type == "response.output_text.delta":
                        delta_text = getattr(event, "delta", "")
                        if delta_text:
                            agent._codex_streamed_text_parts.append(delta_text)
                        if delta_text and not has_tool_calls:
                            if not first_delta_fired:
                                first_delta_fired = True
                                if on_first_delta:
                                    try:
                                        on_first_delta()
                                    except Exception:
                                        pass
                            agent._fire_stream_delta(delta_text)
                    # Track tool calls to suppress text streaming
                    elif "function_call" in event_type:
                        has_tool_calls = True
                    # Fire reasoning callbacks
                    elif "reasoning" in event_type and "delta" in event_type:
                        reasoning_text = getattr(event, "delta", "")
                        if reasoning_text:
                            agent._fire_reasoning_delta(reasoning_text)
                    # Collect completed output items — some backends
                    # (chatgpt.com/backend-api/codex) stream valid items
                    # via response.output_item.done but the SDK's
                    # get_final_response() returns an empty output list.
                    elif event_type == "response.output_item.done":
                        done_item = getattr(event, "item", None)
                        if done_item is not None:
                            collected_output_items.append(done_item)
                    # Log non-completed terminal events for diagnostics
                    elif event_type in {"response.incomplete", "response.failed"}:
                        resp_obj = getattr(event, "response", None)
                        status = getattr(resp_obj, "status", None) if resp_obj else None
                        incomplete_details = getattr(resp_obj, "incomplete_details", None) if resp_obj else None
                        logger.warning(
                            "Codex Responses stream received terminal event %s "
                            "(status=%s, incomplete_details=%s, streamed_chars=%d). %s",
                            event_type, status, incomplete_details,
                            sum(len(p) for p in agent._codex_streamed_text_parts),
                            agent._client_log_context(),
                        )
                final_response = stream.get_final_response()
                # PATCH: ChatGPT Codex backend streams valid output items
                # but get_final_response() can return an empty output list.
                # Backfill from collected items or synthesize from deltas.
                _backfill_codex_stream_output(
                    final_response,
                    collected_output_items,
                    agent._codex_streamed_text_parts,
                    has_tool_calls=has_tool_calls,
                    log_label="Codex stream",
                )
                return final_response
        except TypeError as exc:
            if not _is_null_output_stream_error(exc):
                raise
            final_response = _synthesize_codex_stream_response(
                collected_output_items,
                agent._codex_streamed_text_parts,
                has_tool_calls=has_tool_calls,
            )
            if final_response is None:
                raise
            logger.debug(
                "Codex stream: synthesized output after SDK null-output failure. %s error=%s",
                agent._client_log_context(),
                exc,
            )
            return final_response
        except (_httpx.RemoteProtocolError, _httpx.ReadTimeout, _httpx.ConnectError, ConnectionError) as exc:
            if attempt < max_stream_retries:
                logger.debug(
                    "Codex Responses stream transport failed (attempt %s/%s); retrying. %s error=%s",
                    attempt + 1,
                    max_stream_retries + 1,
                    agent._client_log_context(),
                    exc,
                )
                continue
            logger.debug(
                "Codex Responses stream transport failed; falling back to create(stream=True). %s error=%s",
                agent._client_log_context(),
                exc,
            )
            return agent._run_codex_create_stream_fallback(api_kwargs, client=active_client)
        except RuntimeError as exc:
            err_text = str(exc)
            missing_completed = "response.completed" in err_text
            # The OpenAI SDK's Responses streaming state machine raises
            # ``RuntimeError("Expected to have received `response.created`
            # before `<event-type>`")`` when the first SSE event from the
            # server is anything other than ``response.created`` — and it
            # discards the event's payload before we can read it.  Three
            # real-world backends emit a different first frame:
            #
            #   * xAI on grok-4.x OAuth — sends ``error`` (issues
            #     reported around the May 2026 SuperGrok rollout when
            #     multi-turn conversations replay encrypted reasoning
            #     content the OAuth tier rejects)
            #   * codex-lb relays — send ``codex.rate_limits`` (#14634)
            #   * custom Responses relays — send ``response.in_progress``
            #     (#8133)
            #
            # In all three cases the underlying byte stream is still
            # readable: a non-stream ``responses.create(stream=True)``
            # fallback succeeds and surfaces the real provider error as
            # a normal exception with body+status_code attached, which
            # ``_summarize_api_error`` can then translate into a useful
            # user-facing line.  Treat ``response.created`` prelude
            # errors the same way we already treat ``response.completed``
            # postlude errors.
            prelude_error = (
                "Expected to have received `response.created`" in err_text
                or "Expected to have received \"response.created\"" in err_text
            )
            if (missing_completed or prelude_error) and attempt < max_stream_retries:
                logger.debug(
                    "Responses stream %s (attempt %s/%s); retrying. %s",
                    "prelude rejected" if prelude_error else "closed before completion",
                    attempt + 1,
                    max_stream_retries + 1,
                    agent._client_log_context(),
                )
                continue
            if missing_completed or prelude_error:
                logger.debug(
                    "Responses stream %s; falling back to create(stream=True). %s err=%s",
                    "rejected before response.created" if prelude_error else "did not emit response.completed",
                    agent._client_log_context(),
                    err_text,
                )
                return agent._run_codex_create_stream_fallback(api_kwargs, client=active_client)
            raise



def run_codex_create_stream_fallback(agent, api_kwargs: dict, client: Any = None):
    """Fallback path for stream completion edge cases on Codex-style Responses backends."""
    active_client = client or agent._ensure_primary_openai_client(reason="codex_create_stream_fallback")
    fallback_kwargs = dict(api_kwargs)
    fallback_kwargs["stream"] = True
    fallback_kwargs = agent._get_transport().preflight_kwargs(fallback_kwargs, allow_stream=True)
    stream_or_response = active_client.responses.create(**fallback_kwargs)

    # Compatibility shim for mocks or providers that still return a concrete response.
    if hasattr(stream_or_response, "output"):
        return stream_or_response
    if not hasattr(stream_or_response, "__iter__"):
        return stream_or_response

    terminal_response = None
    collected_output_items: list = []
    collected_text_deltas: list = []
    has_tool_calls = False
    try:
        for event in stream_or_response:
            agent._touch_activity("receiving stream response")
            event_type = getattr(event, "type", None)
            if not event_type and isinstance(event, dict):
                event_type = event.get("type")

            # ``error`` SSE frames carry the provider's real failure
            # reason (subscription / quota / model-not-available /
            # rejected-reasoning-replay) but never appear in the
            # ``{completed, incomplete, failed}`` terminal set, so the
            # raw loop below would silently consume them and end with
            # "did not emit a terminal response".  xAI in particular
            # emits ``type=error`` as the FIRST frame for OAuth
            # accounts whose Grok subscription is missing/exhausted —
            # the SDK's stream helper raises ``RuntimeError(Expected
            # to have received response.created before error)`` which
            # the caller catches and routes here, expecting this
            # fallback to surface the message.  Synthesize an
            # APIError-shaped exception so ``_summarize_api_error``
            # and the credential-pool entitlement detector see the
            # real text instead of a generic RuntimeError.
            if event_type == "error":
                err_message = getattr(event, "message", None)
                if not err_message and isinstance(event, dict):
                    err_message = event.get("message")
                err_code = getattr(event, "code", None)
                if not err_code and isinstance(event, dict):
                    err_code = event.get("code")
                err_param = getattr(event, "param", None)
                if not err_param and isinstance(event, dict):
                    err_param = event.get("param")
                err_message = (err_message or "stream emitted error event").strip()
                from run_agent import _StreamErrorEvent
                raise _StreamErrorEvent(err_message, code=err_code, param=err_param)

            # Collect output items and text deltas for backfill
            if event_type == "response.output_item.done":
                done_item = getattr(event, "item", None)
                if done_item is None and isinstance(event, dict):
                    done_item = event.get("item")
                if done_item is not None:
                    collected_output_items.append(done_item)
            elif event_type in {"response.output_text.delta",}:
                delta = getattr(event, "delta", "")
                if not delta and isinstance(event, dict):
                    delta = event.get("delta", "")
                if delta:
                    collected_text_deltas.append(delta)
            elif event_type and "function_call" in event_type:
                has_tool_calls = True

            if event_type not in {"response.completed", "response.incomplete", "response.failed"}:
                continue

            terminal_response = getattr(event, "response", None)
            if terminal_response is None and isinstance(event, dict):
                terminal_response = event.get("response")
            if terminal_response is not None:
                # Backfill empty output from collected stream events
                _backfill_codex_stream_output(
                    terminal_response,
                    collected_output_items,
                    collected_text_deltas,
                    has_tool_calls=has_tool_calls,
                    log_label="Codex fallback stream",
                )
                return terminal_response
    finally:
        close_fn = getattr(stream_or_response, "close", None)
        if callable(close_fn):
            try:
                close_fn()
            except Exception:
                pass

    if terminal_response is not None:
        return terminal_response
    raise RuntimeError("Responses create(stream=True) fallback did not emit a terminal response.")



__all__ = [
    "run_codex_app_server_turn",
    "run_codex_stream",
    "run_codex_create_stream_fallback",
]
