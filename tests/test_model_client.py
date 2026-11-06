from __future__ import annotations

import httpx

from repofix.config import ModelConfig
from repofix.model_client import OpenAICompatibleClient


async def test_model_client_retries_transient_transport_failure(monkeypatch):
    class FakeAsyncClient:
        attempts = 0

        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, **kwargs):
            type(self).attempts += 1
            request = httpx.Request("POST", url)
            if self.attempts == 1:
                raise httpx.ConnectError("temporary disconnect", request=request)
            return httpx.Response(
                200,
                request=request,
                json={
                    "choices": [{"message": {"content": "done", "tool_calls": []}}],
                    "usage": {"completion_tokens": 1},
                },
            )

    async def no_wait(_delay):
        return None

    monkeypatch.setattr("repofix.model_client.httpx.AsyncClient", FakeAsyncClient)
    monkeypatch.setattr("repofix.model_client.asyncio.sleep", no_wait)
    client = OpenAICompatibleClient(ModelConfig(api_key="test", max_retries=1))

    response = await client.complete([{"role": "user", "content": "hi"}], [])

    assert response.text == "done"
    assert response.usage == {"completion_tokens": 1}
    assert FakeAsyncClient.attempts == 2
