from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Any

from repofix.context import ContextManager, handoff_summary
from repofix.hooks import HookManager
from repofix.model_client import ModelClient
from repofix.models import (
    Budget,
    ModelResponse,
    SessionRecord,
    SessionStatus,
    TaskSpec,
    ToolCall,
    ToolResult,
)
from repofix.policy import PolicyEngine
from repofix.sandbox import DockerExecutor, Executor, LocalExecutor
from repofix.skills import SkillLoader
from repofix.store import SessionStore
from repofix.tools import ToolRegistry, tool_message, validate_tool_call
from repofix.verifier import IndependentVerifier
from repofix.workspace import WorkspaceManager


class HarnessError(RuntimeError):
    pass


class BudgetExceeded(HarnessError):
    pass


class AgentHarness:
    def __init__(
        self,
        store: SessionStore,
        model: ModelClient,
        *,
        docker_image: str = "repofix-runner:py312",
        hook_manager: HookManager | None = None,
    ):
        self.store = store
        self.model = model
        self.docker_image = docker_image
        self.hooks = hook_manager or HookManager()

    @classmethod
    def create_session(
        cls,
        store: SessionStore,
        source_repo: Path,
        task: TaskSpec,
        *,
        model: str,
        executor: str = "docker",
        budget: Budget | None = None,
    ) -> SessionRecord:
        manager = WorkspaceManager(store.paths)
        source_kind = "git" if manager.is_git_repository(source_repo) else "folder"
        identifier, workspace, base = manager.create(source_repo)
        session = SessionRecord(
            id=identifier,
            source_repo=str(source_repo.resolve()),
            source_kind=source_kind,
            workspace_source=str(manager.source_repository(workspace)),
            workspace=str(workspace),
            base_commit=base,
            task=task,
            budget=budget or Budget(),
            model=model,
            executor=executor,
        )
        store.create(session)
        return session

    def _executor(self, session: SessionRecord) -> Executor:
        workspace = Path(session.workspace)
        if session.executor == "docker":
            return DockerExecutor(
                workspace, image=self.docker_image, network=session.task.allowed_network
            )
        return LocalExecutor(workspace)

    def _skills(self, workspace: Path) -> SkillLoader:
        builtins = Path(__file__).parent / "builtin_skills"
        return SkillLoader([workspace / ".repofix" / "skills", workspace / "skills", builtins])

    def _load_messages(self, session: SessionRecord) -> list[dict[str, Any]]:
        message_events = [
            event.payload["message"]
            for event in self.store.events(session.id)
            if event.kind == "message" and "message" in event.payload
        ]
        if message_events:
            return message_events
        skills = self._skills(Path(session.workspace))
        messages = ContextManager.initial(session.task, Path(session.workspace), skills.catalog())
        for message in messages:
            self.store.append_event(session.id, "message", {"message": message})
        return messages

    @staticmethod
    def _repair_missing_tool_messages(
        messages: list[dict[str, Any]], pending_ids: set[str]
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Keep resumed Chat Completions history protocol-valid without replaying side effects."""
        observed = {
            str(message.get("tool_call_id"))
            for message in messages
            if message.get("role") == "tool" and message.get("tool_call_id")
        }
        repaired: list[dict[str, Any]] = []
        inserted: list[str] = []
        for message in messages:
            repaired.append(message)
            if message.get("role") != "assistant":
                continue
            for raw_call in message.get("tool_calls") or []:
                call_id = str(raw_call.get("id", ""))
                if not call_id or call_id in observed:
                    continue
                was_started = call_id in pending_ids
                payload = {
                    "tool_call_id": call_id,
                    "name": raw_call.get("function", {}).get("name", "unknown"),
                    "status": "indeterminate" if was_started else "cancelled",
                    "summary": (
                        "Execution started but no durable result was recorded; inspect state before retrying"
                        if was_started
                        else "The harness did not start this requested call before interruption"
                    ),
                }
                repaired.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": json.dumps(payload, ensure_ascii=False),
                    }
                )
                observed.add(call_id)
                inserted.append(call_id)
        return repaired, inserted

    def _append_message(self, session_id: str, messages: list[dict], message: dict) -> None:
        messages.append(message)
        self.store.append_event(session_id, "message", {"message": message})

    @staticmethod
    def _assistant_message(response: ModelResponse) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": response.text or None}
        if response.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in response.tool_calls
            ]
        return message

    async def run(self, session_id: str) -> SessionRecord:
        session = self.store.get(session_id)
        if session.status == SessionStatus.COMPLETED:
            return session
        if session.status in {SessionStatus.CANCELLED, SessionStatus.CLEANED}:
            raise HarnessError(f"Session in terminal state: {session.status.value}")
        messages = self._load_messages(session)
        indeterminate = self.store.indeterminate_tools(session_id)
        messages, repaired_ids = self._repair_missing_tool_messages(
            messages, {item["id"] for item in indeterminate}
        )
        if repaired_ids:
            self.store.append_event(
                session_id, "recovery_protocol_repaired", {"tool_call_ids": repaired_ids}
            )
        if indeterminate:
            notice = (
                "Recovery notice: these tool calls started but have no durable result and were not replayed: "
                + json.dumps(indeterminate, ensure_ascii=False)
                + ". Inspect repository state before deciding what to do."
            )
            self._append_message(session_id, messages, {"role": "user", "content": notice})
            self.store.append_event(session_id, "recovery_indeterminate", {"tools": indeterminate})
            for pending in indeterminate:
                self.store.mark_tool_indeterminate(session_id, pending["id"])

        session.status = SessionStatus.RUNNING
        session.last_error = None
        self.store.save(session)
        self.store.append_event(session_id, "run_started", {"resume": session.turns > 0})
        started = time.monotonic()
        workspace = Path(session.workspace)
        context = ContextManager(session.budget.max_context_chars)
        registry = ToolRegistry(
            workspace,
            self._executor(session),
            PolicyEngine(workspace, session.task),
            self.store,
            session.id,
            self._skills(workspace),
            self.hooks,
            session.budget.max_tool_output_chars,
            session.base_commit,
        )
        repeated: dict[str, int] = {}
        try:
            while True:
                self._check_budget(session, started)
                if self.store.cancellation_requested(session_id):
                    session.status = SessionStatus.CANCELLED
                    self.store.save(session)
                    self.store.append_event(session_id, "run_cancelled", {})
                    return session
                messages = context.compact(messages, self.store.events(session_id))
                self.store.append_event(
                    session_id,
                    "model_requested",
                    {"turn": session.turns + 1, "context_messages": len(messages)},
                )
                response = await self.model.complete(messages, registry.schemas())
                session.turns += 1
                self.store.save(session)
                self.store.append_event(
                    session_id,
                    "model_responded",
                    {
                        "text": response.text,
                        "tool_calls": [call.model_dump() for call in response.tool_calls],
                        "usage": response.usage,
                    },
                )
                self._append_message(session_id, messages, self._assistant_message(response))
                if not response.tool_calls:
                    feedback = {
                        "role": "user",
                        "content": "No tool call was made. Continue working or call submit_result for verification.",
                    }
                    self._append_message(session_id, messages, feedback)
                    continue
                completed = False
                for call in response.tool_calls:
                    self._check_budget(session, started)
                    if session.tool_calls >= session.budget.max_tool_calls:
                        raise BudgetExceeded("Tool-call budget exhausted")
                    fingerprint = self._fingerprint(call)
                    repeated[fingerprint] = repeated.get(fingerprint, 0) + 1
                    if repeated[fingerprint] > session.budget.max_repeated_call:
                        result = ToolResult(
                            tool_call_id=call.id,
                            name=call.name,
                            status="denied",
                            summary="Repeated identical tool call blocked as a no-progress loop",
                        )
                        self.store.append_event(
                            session_id, "loop_guard", result.model_dump(mode="json")
                        )
                        self.store.start_tool(session_id, call.id, call.name, call.arguments)
                        self.store.finish_tool(session_id, call.id, result.model_dump(mode="json"))
                    elif call.name == "submit_result":
                        result, completed = await self._execute_submit(session, call)
                    else:
                        result = await registry.execute(call)
                    session.tool_calls += 1
                    self.store.save(session)
                    self._append_message(session_id, messages, tool_message(result))
                    if completed:
                        break
                if completed:
                    session.status = SessionStatus.COMPLETED
                    self.store.save(session)
                    self.store.append_event(session_id, "run_completed", session.verification or {})
                    return session
        except asyncio.CancelledError:
            if self.store.cancellation_requested(session_id):
                session.status = SessionStatus.CANCELLED
                self.store.save(session)
                self.store.append_event(
                    session_id, "run_cancelled", {"reason": "cancelled by user"}
                )
            else:
                session.status = SessionStatus.PAUSED
                self.store.save(session)
                self._write_handoff(session)
                self.store.append_event(
                    session_id, "run_paused", {"reason": "cancelled by controller"}
                )
            raise
        except KeyboardInterrupt:
            session.status = SessionStatus.PAUSED
            self.store.save(session)
            self._write_handoff(session)
            self.store.append_event(session_id, "run_paused", {"reason": "keyboard interrupt"})
            return session
        except Exception as exc:
            session.status = SessionStatus.PAUSED
            session.last_error = f"{type(exc).__name__}: {exc}"
            self.store.save(session)
            self._write_handoff(session)
            self.store.append_event(session_id, "run_paused", {"reason": session.last_error})
            return session

    async def _submit(self, session: SessionRecord, call: ToolCall) -> tuple[ToolResult, bool]:
        await self.hooks.before_verify()
        self.store.append_event(
            session.id, "verification_started", {"summary": call.arguments.get("summary", "")}
        )
        verifier = IndependentVerifier(
            Path(session.workspace),
            session.task,
            self.store,
            session.id,
            session.executor,
            self.docker_image,
            session.base_commit,
        )
        verification = await verifier.verify()
        for index, command_result in enumerate(verification.command_results, 1):
            if len(command_result.output) > session.budget.max_tool_output_chars:
                artifact = self.store.write_artifact(
                    session.id, f"verification-{index}.log", command_result.output
                )
                limit = session.budget.max_tool_output_chars // 2
                command_result.output = (
                    command_result.output[:limit]
                    + f"\n\n... full verification output archived at {artifact} ...\n\n"
                    + command_result.output[-limit:]
                )
                command_result.artifact = artifact
                command_result.summary += "; output was truncated and archived"
        session.verification = verification.model_dump(mode="json")
        self.store.save(session)
        self.store.append_event(session.id, "verification_finished", session.verification)
        output = json.dumps(session.verification, ensure_ascii=False)
        return (
            ToolResult(
                tool_call_id=call.id,
                name=call.name,
                status="ok" if verification.passed else "error",
                summary=verification.summary,
                output=output,
                artifact=verification.patch_path,
                changed_files=verification.changed_files,
            ),
            verification.passed,
        )

    async def _execute_submit(
        self, session: SessionRecord, call: ToolCall
    ) -> tuple[ToolResult, bool]:
        self.store.start_tool(session.id, call.id, call.name, call.arguments)
        try:
            call = validate_tool_call(call)
        except Exception as exc:
            result = ToolResult(
                tool_call_id=call.id,
                name=call.name,
                status="error",
                summary=f"{type(exc).__name__}: {exc}",
            )
            self.store.finish_tool(session.id, call.id, result.model_dump(mode="json"))
            return result, False
        result, completed = await self._submit(session, call)
        self.store.finish_tool(session.id, call.id, result.model_dump(mode="json"))
        return result, completed

    @staticmethod
    def _fingerprint(call: ToolCall) -> str:
        raw = json.dumps([call.name, call.arguments], sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()

    @staticmethod
    def _check_budget(session: SessionRecord, started: float) -> None:
        if session.turns >= session.budget.max_turns:
            raise BudgetExceeded("Turn budget exhausted")
        if time.monotonic() - started >= session.budget.max_wall_seconds:
            raise BudgetExceeded("Active wall-time budget exhausted")

    def _write_handoff(self, session: SessionRecord) -> None:
        content = handoff_summary(
            self.store.events(session.id), Path(session.workspace), session.task
        )
        path = self.store.write_artifact(session.id, "handoff.md", content)
        self.store.append_event(session.id, "handoff_written", {"path": path})
