"""OpenAI Codex upstream adapter for the local proxy.

Codex OAuth credentials live in Hermes' auth store, but the upstream speaks
the Responses API rather than Chat Completions.  This adapter exposes a small
OpenAI-compatible surface for local consumers that only know how to call
``/v1/chat/completions``.
"""

from __future__ import annotations

import json
import logging
import os
import time
from types import SimpleNamespace
from typing import Any, FrozenSet

import aiohttp
from aiohttp import web
from openai import AsyncOpenAI

from agent.auxiliary_client import _codex_cloudflare_headers
from agent.codex_responses_adapter import (
    _chat_messages_to_responses_input,
    _normalize_codex_response,
    _responses_tools,
)
from hermes_cli.auth import (
    DEFAULT_CODEX_BASE_URL,
    get_codex_auth_status,
    resolve_codex_runtime_credentials,
)
from hermes_cli.codex_models import get_codex_model_ids
from hermes_cli.proxy.adapters.base import UpstreamAdapter, UpstreamCredential
from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

_ALLOWED_PATHS: FrozenSet[str] = frozenset({
    "/chat/completions",
    "/models",
})
_REASONING_EFFORTS: FrozenSet[str] = frozenset({
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
})
_SERVICE_TIERS: FrozenSet[str] = frozenset({
    "auto",
    "default",
    "flex",
    "priority",
})
_SPEED_SERVICE_TIERS = {
    "normal": "default",
    "fast": "priority",
}
_MODEL_COOLDOWNS_ENV = "HERMES_CODEX_PROXY_MODEL_COOLDOWNS"
_MODEL_COOLDOWNS_STATE = "codex-proxy-model-cooldowns.json"


def _to_namespace(value: Any) -> Any:
    if isinstance(value, dict):
        return SimpleNamespace(**{k: _to_namespace(v) for k, v in value.items()})
    if isinstance(value, list):
        return [_to_namespace(item) for item in value]
    return value


def _chat_json_error(status: int, message: str, code: str = "proxy_error") -> web.Response:
    return web.json_response(
        {"error": {"message": message, "type": code, "code": code}},
        status=status,
    )


def _parse_model_cooldown_rules(raw: str | None = None) -> dict[str, tuple[str, int]]:
    """Parse source:fallback:seconds cooldown rules from env.

    Example:
        HERMES_CODEX_PROXY_MODEL_COOLDOWNS=gpt-5.3-codex-spark:gpt-5.4-mini:3600

    Rules are opt-in so generic proxy users do not inherit local Sligo Labs
    routing policy.
    """
    value = os.getenv(_MODEL_COOLDOWNS_ENV, "") if raw is None else raw
    rules: dict[str, tuple[str, int]] = {}
    for item in value.split(","):
        parts = [part.strip() for part in item.split(":")]
        if len(parts) != 3 or not all(parts):
            continue
        source, fallback, seconds_raw = parts
        try:
            seconds = int(seconds_raw)
        except ValueError:
            continue
        if seconds <= 0 or source == fallback:
            continue
        rules[source] = (fallback, seconds)
    return rules


def _resolve_codex_proxy_credentials(*, refresh_if_expiring: bool) -> dict[str, Any]:
    """Resolve Codex credentials for the proxy, preferring the credential pool.

    ``hermes auth add openai-codex`` stores device-code credentials in the
    provider pool.  The older ``resolve_codex_runtime_credentials`` path reads
    only legacy provider state, so a proxy service should check the same pooled
    status that ``hermes auth list`` and normal model routing use before falling
    back to legacy tokens.
    """
    status = get_codex_auth_status()
    if status.get("logged_in"):
        token = str(status.get("api_key") or "").strip()
        if token:
            base_url = str(status.get("base_url") or "").strip().rstrip("/")
            if not base_url:
                import os

                base_url = (
                    os.getenv("HERMES_CODEX_BASE_URL", "").strip().rstrip("/")
                    or DEFAULT_CODEX_BASE_URL
                )
            return {
                "api_key": token,
                "base_url": base_url,
                "last_refresh": status.get("last_refresh"),
                "source": status.get("source"),
            }

    return resolve_codex_runtime_credentials(
        refresh_if_expiring=refresh_if_expiring,
    )


class OpenAICodexAdapter(UpstreamAdapter):
    """Proxy upstream for OpenAI Codex OAuth credentials."""

    @property
    def name(self) -> str:
        return "openai-codex"

    @property
    def display_name(self) -> str:
        return "OpenAI Codex"

    @property
    def allowed_paths(self) -> FrozenSet[str]:
        return _ALLOWED_PATHS

    def is_authenticated(self) -> bool:
        try:
            creds = _resolve_codex_proxy_credentials(refresh_if_expiring=False)
        except Exception:
            return False
        return bool(str(creds.get("api_key") or "").strip())

    def get_credential(self) -> UpstreamCredential:
        creds = _resolve_codex_proxy_credentials(refresh_if_expiring=True)
        token = str(creds.get("api_key") or "").strip()
        base_url = str(creds.get("base_url") or "").strip().rstrip("/")
        if not token or not base_url:
            raise RuntimeError(
                "OpenAI Codex credentials are not usable. "
                "Run `hermes auth add openai-codex` to re-authenticate."
            )
        return UpstreamCredential(bearer=token, base_url=base_url)

    @staticmethod
    def _model_cooldown_state_path():
        return get_hermes_home() / "state" / _MODEL_COOLDOWNS_STATE

    @classmethod
    def _load_model_cooldown_state(cls) -> dict[str, Any]:
        path = cls._model_cooldown_state_path()
        try:
            data = json.loads(path.read_text())
        except FileNotFoundError:
            return {}
        except Exception as exc:
            logger.debug("proxy: ignoring unreadable model cooldown state %s: %s", path, exc)
            return {}
        return data if isinstance(data, dict) else {}

    @classmethod
    def _write_model_cooldown_state(cls, state: dict[str, Any]) -> None:
        path = cls._model_cooldown_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
        tmp.replace(path)

    @classmethod
    def _effective_model_for_cooldown(cls, requested_model: str) -> str:
        rules = _parse_model_cooldown_rules()
        rule = rules.get(requested_model)
        if rule is None:
            return requested_model

        fallback_model, _seconds = rule
        state = cls._load_model_cooldown_state()
        models_obj = state.get("models")
        models = models_obj if isinstance(models_obj, dict) else {}
        entry = models.get(requested_model)
        if not isinstance(entry, dict):
            return requested_model

        expires_at = float(entry.get("expires_at") or 0)
        if expires_at <= time.time():
            models.pop(requested_model, None)
            state["models"] = models
            try:
                cls._write_model_cooldown_state(state)
            except Exception as exc:
                logger.debug("proxy: failed to prune expired model cooldown: %s", exc)
            return requested_model

        configured_fallback = str(entry.get("fallback_model") or "")
        if configured_fallback and configured_fallback != fallback_model:
            return requested_model

        logger.warning(
            "proxy: routing %s to %s because %s is cooling down for %.0fs",
            requested_model,
            fallback_model,
            requested_model,
            max(0.0, expires_at - time.time()),
        )
        return fallback_model

    @classmethod
    def _mark_model_cooldown(cls, failed_model: str, exc: Exception) -> None:
        rules = _parse_model_cooldown_rules()
        rule = rules.get(failed_model)
        if rule is None:
            return
        fallback_model, seconds = rule
        now = time.time()
        state = cls._load_model_cooldown_state()
        models = state.get("models") if isinstance(state.get("models"), dict) else {}
        if not isinstance(models, dict):
            models = {}
        models[failed_model] = {
            "fallback_model": fallback_model,
            "cooldown_seconds": seconds,
            "failed_at": now,
            "expires_at": now + seconds,
            "last_error": str(exc)[:500],
        }
        state["models"] = models
        try:
            cls._write_model_cooldown_state(state)
            logger.warning(
                "proxy: cooling down %s for %ss after upstream failure; routing future requests to %s",
                failed_model,
                seconds,
                fallback_model,
            )
        except Exception as write_exc:
            logger.warning("proxy: failed to write model cooldown state: %s", write_exc)

    async def handle_proxy_request(self, request: web.Request) -> web.StreamResponse:
        rel_path = "/" + request.match_info.get("tail", "").lstrip("/")
        if rel_path == "/models":
            return await self._handle_models()
        if rel_path == "/chat/completions":
            return await self._handle_chat_completions(request)
        allowed = ", ".join(sorted(self.allowed_paths))
        return _chat_json_error(
            404,
            f"Path /v1{rel_path} is not forwarded by this proxy. Allowed: {allowed}",
            code="path_not_allowed",
        )

    async def _handle_models(self) -> web.Response:
        try:
            cred = self.get_credential()
            models = get_codex_model_ids(access_token=cred.bearer)
        except Exception as exc:
            logger.warning("proxy: Codex model list failed: %s", exc)
            return _chat_json_error(401, str(exc), code="upstream_auth_failed")
        return web.json_response({
            "object": "list",
            "data": [
                {"id": model, "object": "model", "owned_by": "openai-codex"}
                for model in models
            ],
        })

    async def _handle_chat_completions(self, request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except Exception:
            return _chat_json_error(400, "Request body must be JSON.", code="invalid_request")
        if not isinstance(payload, dict):
            return _chat_json_error(400, "Request body must be a JSON object.", code="invalid_request")
        wants_stream = payload.get("stream") is True

        try:
            cred = self.get_credential()
        except Exception as exc:
            logger.warning("proxy: Codex credential resolution failed: %s", exc)
            return _chat_json_error(401, str(exc), code="upstream_auth_failed")

        model = str(payload.get("model") or "").strip()
        if not model:
            return _chat_json_error(400, "Missing required field: model.", code="invalid_request")
        requested_model = model
        model = self._effective_model_for_cooldown(requested_model)

        messages = payload.get("messages")
        if not isinstance(messages, list):
            return _chat_json_error(400, "Missing required field: messages.", code="invalid_request")

        instructions, payload_messages = self._split_instructions(messages)
        responses_payload: dict[str, Any] = {
            "model": model,
            "instructions": instructions,
            "input": _chat_messages_to_responses_input(payload_messages),
            "store": False,
            "parallel_tool_calls": True,
            "tool_choice": "auto",
        }

        tools = _responses_tools(payload.get("tools"))
        if tools:
            responses_payload["tools"] = tools
            if payload.get("tool_choice") is not None:
                responses_payload["tool_choice"] = payload["tool_choice"]

        try:
            reasoning = self._responses_reasoning(payload.get("reasoning_effort"))
            service_tier = self._responses_service_tier(payload)
        except ValueError as exc:
            return _chat_json_error(400, str(exc), code="invalid_request")
        if reasoning:
            responses_payload["reasoning"] = reasoning
            responses_payload["include"] = ["reasoning.encrypted_content"]
        if service_tier:
            responses_payload["service_tier"] = service_tier

        text_format = self._responses_text_format(payload.get("response_format"))
        if text_format:
            responses_payload["text"] = {"format": text_format}

        try:
            response_obj = await self._run_responses_stream_with_retry(responses_payload, cred)
            response_json = response_obj.model_dump() if hasattr(response_obj, "model_dump") else {}
        except aiohttp.ClientError as exc:
            logger.warning("proxy: Codex upstream connection failed: %s", exc)
            self._mark_model_cooldown(requested_model, exc)
            return _chat_json_error(502, f"upstream connection failed: {exc}", code="upstream_unreachable")
        except Exception as exc:
            logger.warning("proxy: Codex upstream response failed: %s", exc)
            self._mark_model_cooldown(requested_model, exc)
            return _chat_json_error(502, f"upstream response failed: {exc}", code="upstream_invalid_response")

        try:
            msg, finish_reason = _normalize_codex_response(response_obj)
        except Exception as exc:
            logger.warning("proxy: Codex response normalization failed: %s", exc)
            self._mark_model_cooldown(requested_model, exc)
            return _chat_json_error(502, f"upstream response normalization failed: {exc}", code="upstream_invalid_response")

        choice_message: dict[str, Any] = {
            "role": "assistant",
            "content": msg.content or "",
        }
        if msg.tool_calls:
            choice_message["content"] = None
            choice_message["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in msg.tool_calls
            ]

        usage = response_json.get("usage") if isinstance(response_json, dict) else {}
        prompt_tokens = int((usage or {}).get("input_tokens") or 0)
        completion_tokens = int((usage or {}).get("output_tokens") or 0)
        completion = {
            "id": response_json.get("id", "chatcmpl-codex-proxy"),
            "object": "chat.completion",
            "created": response_json.get("created_at", 0),
            "model": model,
            "choices": [{
                "index": 0,
                "message": choice_message,
                "finish_reason": finish_reason,
            }],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }
        if wants_stream:
            return await self._stream_chat_completion(request, completion)
        return web.json_response(completion)

    @staticmethod
    def _chat_completion_stream_chunks(completion: dict[str, Any]) -> list[bytes]:
        """Serialize a completed chat response as OpenAI-style SSE chunks.

        The Codex upstream is already consumed via the Responses streaming API so
        the proxy can normalize tool calls. Some local clients, including
        OpenCode, always request ``stream: true`` from OpenAI-compatible
        providers.  Emit a short, delayed-until-complete SSE stream rather than
        rejecting those clients.
        """
        chunk_base = {
            "id": completion.get("id", "chatcmpl-codex-proxy"),
            "object": "chat.completion.chunk",
            "created": completion.get("created") or int(time.time()),
            "model": completion.get("model", ""),
        }
        choice = (completion.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        finish_reason = choice.get("finish_reason") or "stop"

        chunks: list[dict[str, Any]] = []
        chunks.append({
            **chunk_base,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        })
        content = message.get("content")
        if content:
            chunks.append({
                **chunk_base,
                "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
            })
        for index, tool_call in enumerate(message.get("tool_calls") or []):
            streamed_call = dict(tool_call)
            streamed_call["index"] = index
            chunks.append({
                **chunk_base,
                "choices": [{
                    "index": 0,
                    "delta": {"tool_calls": [streamed_call]},
                    "finish_reason": None,
                }],
            })
        chunks.append({
            **chunk_base,
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
        })

        return [f"data: {json.dumps(chunk, separators=(',', ':'))}\n\n".encode("utf-8") for chunk in chunks] + [b"data: [DONE]\n\n"]

    @classmethod
    async def _stream_chat_completion(
        cls,
        request: web.Request,
        completion: dict[str, Any],
    ) -> web.StreamResponse:
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )
        await response.prepare(request)
        for chunk in cls._chat_completion_stream_chunks(completion):
            await response.write(chunk)
        await response.write_eof()
        return response

    @staticmethod
    async def _run_responses_stream_with_retry(
        responses_payload: dict[str, Any],
        cred: UpstreamCredential,
    ) -> Any:
        for attempt in range(2):
            try:
                return await OpenAICodexAdapter._run_responses_stream(
                    responses_payload,
                    cred,
                )
            except TypeError as exc:
                if attempt or not OpenAICodexAdapter._is_null_output_stream_error(exc):
                    raise
                logger.debug(
                    "proxy: retrying Codex stream after SDK null-output failure without recoverable output: %s",
                    exc,
                )
        raise RuntimeError("unreachable Codex stream retry state")

    @staticmethod
    async def _run_responses_stream(
        responses_payload: dict[str, Any],
        cred: UpstreamCredential,
    ) -> Any:
        client = AsyncOpenAI(
            api_key=cred.bearer,
            base_url=cred.base_url,
            default_headers=_codex_cloudflare_headers(cred.bearer),
        )
        collected_output_items: list[Any] = []
        collected_text_deltas: list[str] = []
        has_function_calls = False
        try:
            async with client.responses.stream(**responses_payload) as stream:
                try:
                    async for event in stream:
                        event_type = getattr(event, "type", "")
                        if event_type == "response.output_item.done":
                            item = getattr(event, "item", None)
                            if item is not None:
                                collected_output_items.append(item)
                        elif "output_text.delta" in event_type:
                            delta = getattr(event, "delta", "")
                            if delta:
                                collected_text_deltas.append(delta)
                        elif "function_call" in event_type:
                            has_function_calls = True
                    final = await stream.get_final_response()
                except TypeError as exc:
                    if not OpenAICodexAdapter._is_null_output_stream_error(exc):
                        raise
                    final = OpenAICodexAdapter._synthesize_stream_response(
                        collected_output_items,
                        collected_text_deltas,
                        has_function_calls=has_function_calls,
                    )
                    if final is None:
                        raise
                    logger.debug(
                        "proxy: synthesized Codex stream response after SDK null-output failure: %s",
                        exc,
                    )
        finally:
            await client.close()

        output = getattr(final, "output", None)
        if not isinstance(output, list) or not output:
            synthesized = OpenAICodexAdapter._synthesize_stream_response(
                collected_output_items,
                collected_text_deltas,
                has_function_calls=has_function_calls,
            )
            if synthesized is not None:
                return OpenAICodexAdapter._response_with_synthesized_output(
                    final,
                    synthesized,
                )
        return final

    @staticmethod
    def _is_null_output_stream_error(exc: TypeError) -> bool:
        message = str(exc)
        return "NoneType" in message and "not iterable" in message

    @staticmethod
    def _synthesize_stream_response(
        collected_output_items: list[Any],
        collected_text_deltas: list[str],
        *,
        has_function_calls: bool,
    ) -> Any | None:
        if collected_output_items:
            output = list(collected_output_items)
            text = "".join(collected_text_deltas)
        elif collected_text_deltas and not has_function_calls:
            text = "".join(collected_text_deltas)
            output = [SimpleNamespace(
                type="message",
                role="assistant",
                status="completed",
                content=[SimpleNamespace(
                    type="output_text",
                    text=text,
                )],
            )]
        else:
            return None

        return SimpleNamespace(
            status="completed",
            output=output,
            output_text=text,
        )

    @staticmethod
    def _response_with_synthesized_output(final: Any, synthesized: Any) -> Any:
        output_text = getattr(final, "output_text", None) or getattr(synthesized, "output_text", "")
        attrs = {
            "status": getattr(final, "status", getattr(synthesized, "status", "completed")),
            "output": getattr(synthesized, "output", []),
            "output_text": output_text,
        }
        for name in ("id", "object", "created", "created_at", "model", "usage", "error"):
            if hasattr(final, name):
                attrs[name] = getattr(final, name)

        if hasattr(final, "model_dump"):
            def _model_dump(*args: Any, **kwargs: Any) -> dict[str, Any]:
                data = final.model_dump(*args, **kwargs)
                if not isinstance(data, dict):
                    return {}
                data = dict(data)
                data["output"] = getattr(synthesized, "output", [])
                data["output_text"] = output_text
                return data

            attrs["model_dump"] = _model_dump

        return SimpleNamespace(**attrs)

    @staticmethod
    def _split_instructions(messages: list[Any]) -> tuple[str, list[dict[str, Any]]]:
        instructions: list[str] = []
        rest: list[dict[str, Any]] = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            if msg.get("role") == "system":
                content = msg.get("content")
                if isinstance(content, str) and content.strip():
                    instructions.append(content.strip())
                continue
            rest.append(msg)
        return "\n\n".join(instructions), rest

    @staticmethod
    def _responses_text_format(response_format: Any) -> dict[str, Any] | None:
        if not isinstance(response_format, dict):
            return None
        fmt_type = response_format.get("type")
        if fmt_type == "json_object":
            return {"type": "json_object"}
        if fmt_type != "json_schema":
            return None
        schema_block = response_format.get("json_schema")
        if not isinstance(schema_block, dict):
            return None
        name = schema_block.get("name") or "structured_response"
        schema = schema_block.get("schema")
        if not isinstance(schema, dict):
            return None
        return {
            "type": "json_schema",
            "name": str(name),
            "schema": schema,
            "strict": bool(schema_block.get("strict", False)),
        }

    @staticmethod
    def _responses_reasoning(reasoning_effort: Any) -> dict[str, str] | None:
        effort = OpenAICodexAdapter._normalized_reasoning_effort(reasoning_effort)
        if effort is None:
            return None
        return {"effort": effort, "summary": "auto"}

    @staticmethod
    def _normalized_reasoning_effort(reasoning_effort: Any) -> str | None:
        if reasoning_effort is None:
            return None
        if not isinstance(reasoning_effort, str):
            msg = "reasoning_effort must be a string."
            raise ValueError(msg)
        effort = reasoning_effort.strip().lower()
        if not effort:
            return None
        if effort == "off":
            effort = "none"
        if effort not in _REASONING_EFFORTS:
            allowed = ", ".join(sorted(_REASONING_EFFORTS))
            msg = f"Invalid reasoning_effort: {reasoning_effort!r}. Allowed: {allowed}."
            raise ValueError(msg)
        return effort

    @staticmethod
    def _responses_service_tier(payload: dict[str, Any]) -> str | None:
        service_tier = payload.get("service_tier")
        if service_tier is not None:
            if not isinstance(service_tier, str):
                msg = "service_tier must be a string."
                raise ValueError(msg)
            tier = service_tier.strip().lower()
            if tier:
                if tier not in _SERVICE_TIERS:
                    allowed = ", ".join(sorted(_SERVICE_TIERS))
                    msg = f"Invalid service_tier: {service_tier!r}. Allowed: {allowed}."
                    raise ValueError(msg)
                return tier

        speed = payload.get("speed")
        if speed is None:
            return None
        if not isinstance(speed, str):
            msg = "speed must be a string."
            raise ValueError(msg)
        normalized_speed = speed.strip().lower()
        if not normalized_speed:
            return None
        service_tier = _SPEED_SERVICE_TIERS.get(normalized_speed)
        if service_tier is None:
            allowed = ", ".join(sorted(_SPEED_SERVICE_TIERS))
            msg = f"Invalid speed: {speed!r}. Allowed: {allowed}."
            raise ValueError(msg)
        return service_tier


__all__ = ["OpenAICodexAdapter"]
