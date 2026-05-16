"""OpenAI Codex upstream adapter for the local proxy.

Codex OAuth credentials live in Hermes' auth store, but the upstream speaks
the Responses API rather than Chat Completions.  This adapter exposes a small
OpenAI-compatible surface for local consumers that only know how to call
``/v1/chat/completions``.
"""

from __future__ import annotations

import json
import logging
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
from hermes_cli.auth import resolve_codex_runtime_credentials
from hermes_cli.codex_models import get_codex_model_ids
from hermes_cli.proxy.adapters.base import UpstreamAdapter, UpstreamCredential

logger = logging.getLogger(__name__)

_ALLOWED_PATHS: FrozenSet[str] = frozenset({
    "/chat/completions",
    "/models",
})


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
            creds = resolve_codex_runtime_credentials(refresh_if_expiring=False)
        except Exception:
            return False
        return bool(str(creds.get("api_key") or "").strip())

    def get_credential(self) -> UpstreamCredential:
        creds = resolve_codex_runtime_credentials(refresh_if_expiring=True)
        token = str(creds.get("api_key") or "").strip()
        base_url = str(creds.get("base_url") or "").strip().rstrip("/")
        if not token or not base_url:
            raise RuntimeError(
                "OpenAI Codex credentials are not usable. "
                "Run `hermes auth add openai-codex` to re-authenticate."
            )
        return UpstreamCredential(bearer=token, base_url=base_url)

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
        if payload.get("stream") is True:
            return _chat_json_error(
                400,
                "The OpenAI Codex proxy does not support streaming chat completions yet.",
                code="streaming_not_supported",
            )

        try:
            cred = self.get_credential()
        except Exception as exc:
            logger.warning("proxy: Codex credential resolution failed: %s", exc)
            return _chat_json_error(401, str(exc), code="upstream_auth_failed")

        model = str(payload.get("model") or "").strip()
        if not model:
            return _chat_json_error(400, "Missing required field: model.", code="invalid_request")

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

        reasoning_effort = payload.get("reasoning_effort")
        if isinstance(reasoning_effort, str) and reasoning_effort.strip():
            effort = reasoning_effort.strip()
            if effort == "minimal":
                effort = "low"
            responses_payload["reasoning"] = {"effort": effort, "summary": "auto"}
            responses_payload["include"] = ["reasoning.encrypted_content"]

        text_format = self._responses_text_format(payload.get("response_format"))
        if text_format:
            responses_payload["text"] = {"format": text_format}

        try:
            response_obj = await self._run_responses_stream(responses_payload, cred)
            response_json = response_obj.model_dump() if hasattr(response_obj, "model_dump") else {}
        except aiohttp.ClientError as exc:
            logger.warning("proxy: Codex upstream connection failed: %s", exc)
            return _chat_json_error(502, f"upstream connection failed: {exc}", code="upstream_unreachable")
        except Exception as exc:
            logger.warning("proxy: Codex upstream response failed: %s", exc)
            return _chat_json_error(502, f"upstream response failed: {exc}", code="upstream_invalid_response")

        try:
            msg, finish_reason = _normalize_codex_response(response_obj)
        except Exception as exc:
            logger.warning("proxy: Codex response normalization failed: %s", exc)
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
        return web.json_response({
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
        })

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
        finally:
            await client.close()

        output = getattr(final, "output", None)
        if isinstance(output, list) and not output:
            if collected_output_items:
                final.output = list(collected_output_items)
            elif collected_text_deltas and not has_function_calls:
                final.output = [SimpleNamespace(
                    type="message",
                    role="assistant",
                    status="completed",
                    content=[SimpleNamespace(
                        type="output_text",
                        text="".join(collected_text_deltas),
                    )],
                )]
        return final

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


__all__ = ["OpenAICodexAdapter"]
