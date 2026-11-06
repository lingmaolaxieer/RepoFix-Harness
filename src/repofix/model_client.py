from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from typing import Any

import httpx

from repofix.config import ModelConfig
from repofix.models import ModelResponse, ToolCall


class ModelClient(ABC):
    @abstractmethod
    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict]
    ) -> ModelResponse: ...


class OpenAICompatibleClient(ModelClient):
    def __init__(self, config: ModelConfig):
        if not config.api_key:
            raise ValueError("Set REPOFIX_API_KEY or OPENAI_API_KEY")
        self.config = config

    async def complete(self, messages: list[dict[str, Any]], tools: list[dict]) -> ModelResponse:
        payload = {
            "model": self.config.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "temperature": self.config.temperature,
        }
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
            for attempt in range(self.config.max_retries + 1):
                try:
                    response = await client.post(
                        f"{self.config.base_url}/chat/completions", headers=headers, json=payload
                    )
                    response.raise_for_status()
                    data = response.json()
                    break
                except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                    retryable = not isinstance(exc, httpx.HTTPStatusError) or (
                        exc.response.status_code == 429 or exc.response.status_code >= 500
                    )
                    if not retryable or attempt >= self.config.max_retries:
                        raise
                    await asyncio.sleep(min(2**attempt, 8))
        message = data["choices"][0]["message"]
        calls = []
        for raw in message.get("tool_calls") or []:
            try:
                arguments = json.loads(raw["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                arguments = {"_invalid_json": raw["function"].get("arguments", "")}
            calls.append(ToolCall(id=raw["id"], name=raw["function"]["name"], arguments=arguments))
        usage = data.get("usage") or {}
        return ModelResponse(
            text=message.get("content") or "",
            tool_calls=calls,
            usage={k: int(v) for k, v in usage.items() if isinstance(v, (int, float))},
            raw=data,
        )


class ScriptedModel(ModelClient):
    """Deterministic model used by harness tests and demonstrations."""

    def __init__(self, responses: list[ModelResponse]):
        self.responses = list(responses)
        self.requests: list[list[dict[str, Any]]] = []

    async def complete(self, messages: list[dict[str, Any]], tools: list[dict]) -> ModelResponse:
        self.requests.append(messages)
        if not self.responses:
            raise RuntimeError("Scripted model exhausted")
        return self.responses.pop(0)
