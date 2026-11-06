from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from repofix.model_client import ModelClient, ScriptedModel
from repofix.models import Budget, ModelResponse, SessionStatus, TaskSpec, ToolCall
from repofix.runtime import AgentHarness


async def test_scripted_agent_repairs_and_independently_verifies(store, source_repo: Path):
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(id="read", name="read_file", arguments={"path": "calculator.py"})
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="edit",
                        name="replace_in_file",
                        arguments={
                            "path": "calculator.py",
                            "old_text": "return sum(values) / len(values)",
                            "new_text": "return None if not values else sum(values) / len(values)",
                        },
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="test", name="run_command", arguments={"command": "python -m pytest -q"}
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="submit",
                        name="submit_result",
                        arguments={"summary": "Handle empty input"},
                    )
                ]
            ),
        ]
    )
    hidden = source_repo.parent / "hidden"
    hidden.mkdir()
    (hidden / "test_empty.py").write_text(
        "from calculator import mean\n\ndef test_empty():\n    assert mean([]) is None\n",
        encoding="utf-8",
    )
    task = TaskSpec(
        issue="mean([]) should return None",
        acceptance_commands=["python -m pytest -q"],
        protected_tests=str(hidden),
    )
    session = AgentHarness.create_session(
        store,
        source_repo,
        task,
        model="scripted",
        executor="local",
        budget=Budget(max_turns=10, max_tool_calls=20),
    )
    outcome = await AgentHarness(store, model).run(session.id)
    assert outcome.status == SessionStatus.COMPLETED, (outcome.last_error, outcome.verification)
    assert outcome.verification and outcome.verification["passed"]
    assert outcome.tool_calls == 4
    assert Path(outcome.verification["patch_path"]).exists()
    events = store.events(session.id)
    assert any(event.kind == "verification_finished" for event in events)


async def test_verifier_failure_returns_feedback_and_pauses_on_model_exhaustion(
    store, source_repo: Path
):
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="edit",
                        name="replace_in_file",
                        arguments={
                            "path": "calculator.py",
                            "old_text": "return sum(values) / len(values)",
                            "new_text": "return 999",
                        },
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(id="submit", name="submit_result", arguments={"summary": "bad fix"})
                ]
            ),
        ]
    )
    session = AgentHarness.create_session(
        store,
        source_repo,
        TaskSpec(issue="fix mean", acceptance_commands=["python -m pytest -q"]),
        model="scripted",
        executor="local",
        budget=Budget(max_turns=5),
    )
    outcome = await AgentHarness(store, model).run(session.id)
    assert outcome.status == SessionStatus.PAUSED
    assert outcome.verification and not outcome.verification["passed"]
    assert "Scripted model exhausted" in (outcome.last_error or "")


async def test_cancelled_session_stops_before_model_request(store, source_repo: Path):
    model = ScriptedModel([ModelResponse(text="must not be consumed")])
    session = AgentHarness.create_session(
        store,
        source_repo,
        TaskSpec(issue="cancel this task"),
        model="scripted",
        executor="local",
    )
    store.request_cancel(session.id)

    outcome = await AgentHarness(store, model).run(session.id)

    assert outcome.status == SessionStatus.CANCELLED
    assert model.requests == []
    assert any(event.kind == "run_cancelled" for event in store.events(session.id))


async def test_controller_cancellation_interrupts_model_wait_and_marks_cancelled(
    store, source_repo: Path
):
    class BlockingModel(ModelClient):
        def __init__(self):
            self.started = asyncio.Event()

        async def complete(self, messages, tools):
            self.started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    model = BlockingModel()
    session = AgentHarness.create_session(
        store,
        source_repo,
        TaskSpec(issue="cancel while waiting"),
        model="blocking",
        executor="local",
    )
    task = asyncio.create_task(AgentHarness(store, model).run(session.id))
    await asyncio.wait_for(model.started.wait(), timeout=2)
    store.request_cancel(session.id)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert store.get(session.id).status == SessionStatus.CANCELLED
    assert any(event.kind == "run_cancelled" for event in store.events(session.id))
