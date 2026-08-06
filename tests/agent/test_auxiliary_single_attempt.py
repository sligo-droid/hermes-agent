from unittest.mock import patch

import httpx
import pytest
from openai import AsyncOpenAI

from agent.auxiliary_client import async_call_llm, resolve_provider_client


@pytest.mark.asyncio
async def test_single_attempt_disables_openai_sdk_retries():
    requests = 0

    async def fail_request(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            500,
            request=request,
            json={"error": {"message": "transient failure", "type": "server_error"}},
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(fail_request))
    client = AsyncOpenAI(
        api_key="test-key",
        base_url="https://provider.invalid/v1",
        http_client=http_client,
        max_retries=2,
    )
    try:
        with patch(
            "agent.auxiliary_client._resolve_task_provider_model",
            return_value=(
                "custom",
                "test-model",
                "https://provider.invalid/v1",
                "test-key",
                "chat_completions",
            ),
        ), patch(
            "agent.auxiliary_client._get_cached_client",
            return_value=(client, "test-model"),
        ):
            with pytest.raises(Exception):
                await async_call_llm(
                    task="summary",
                    single_attempt=True,
                    messages=[{"role": "user", "content": "hello"}],
                )
    finally:
        await client.close()

    assert requests == 1


def test_strict_custom_provider_does_not_fall_through_to_api_key_chain():
    fallback_client = object()

    with patch(
        "agent.auxiliary_client._try_custom_endpoint",
        return_value=(None, None),
    ), patch(
        "agent.auxiliary_client._resolve_api_key_provider",
        return_value=(fallback_client, "fallback-model"),
    ) as fallback:
        client, model = resolve_provider_client(
            "custom",
            "selected-model",
            strict_provider=True,
        )

    assert client is None
    assert model is None
    fallback.assert_not_called()
