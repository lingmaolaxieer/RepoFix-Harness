from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from repofix.model_client import ModelClient
from repofix.models import Budget, TaskSpec
from repofix.runtime import AgentHarness
from repofix.store import SessionStore


class EvaluationCase(BaseModel):
    id: str
    repository: str
    task: TaskSpec
    tags: list[str] = Field(default_factory=list)


class EvaluationReport(BaseModel):
    passed: int
    total: int
    pass_rate: float
    total_turns: int
    total_tool_calls: int
    total_input_tokens: int
    total_output_tokens: int
    elapsed_seconds: float
    cases: list[dict[str, Any]]


async def run_evaluation(
    manifest: Path,
    store: SessionStore,
    model: ModelClient,
    *,
    model_name: str,
    executor: str = "docker",
    docker_image: str = "repofix-runner:py312",
    budget: Budget | None = None,
) -> EvaluationReport:
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    cases = [EvaluationCase.model_validate(item) for item in raw]
    results = []
    started = time.monotonic()
    for case in cases:
        repository = Path(case.repository)
        if not repository.is_absolute():
            repository = (manifest.parent / repository).resolve()
        session = AgentHarness.create_session(
            store,
            repository,
            case.task,
            model=model_name,
            executor=executor,
            budget=budget,
        )
        outcome = await AgentHarness(store, model, docker_image=docker_image).run(session.id)
        passed = bool(outcome.verification and outcome.verification.get("passed"))
        usage = [
            event.payload.get("usage", {})
            for event in store.events(outcome.id)
            if event.kind == "model_responded"
        ]
        input_tokens = sum(item.get("prompt_tokens", item.get("input_tokens", 0)) for item in usage)
        output_tokens = sum(
            item.get("completion_tokens", item.get("output_tokens", 0)) for item in usage
        )
        results.append(
            {
                "id": case.id,
                "session_id": outcome.id,
                "passed": passed,
                "status": outcome.status.value,
                "turns": outcome.turns,
                "tool_calls": outcome.tool_calls,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "tags": case.tags,
                "error": outcome.last_error,
            }
        )
    elapsed = time.monotonic() - started
    passed_count = sum(1 for item in results if item["passed"])
    return EvaluationReport(
        passed=passed_count,
        total=len(results),
        pass_rate=passed_count / len(results) if results else 0,
        total_turns=sum(item["turns"] for item in results),
        total_tool_calls=sum(item["tool_calls"] for item in results),
        total_input_tokens=sum(item["input_tokens"] for item in results),
        total_output_tokens=sum(item["output_tokens"] for item in results),
        elapsed_seconds=round(elapsed, 3),
        cases=results,
    )
