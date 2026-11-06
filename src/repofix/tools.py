from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, ConfigDict, Field

from repofix.hooks import HookManager
from repofix.models import ToolCall, ToolResult
from repofix.policy import PolicyEngine, PolicyViolation
from repofix.sandbox import Executor
from repofix.skills import SkillLoader
from repofix.store import SessionStore
from repofix.workspace import WorkspaceManager

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 text file with line numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer", "minimum": 1},
                    "end_line": {"type": "integer", "minimum": 1},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files below a repository path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": "Regex search through repository text files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "path": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 500},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "replace_in_file",
            "description": "Replace one exact text occurrence. Fails unless old_text occurs exactly once.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                },
                "required": ["path", "old_text", "new_text"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create a new UTF-8 text file. Refuses to overwrite an existing file.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a shell command inside the task executor. Use for tests, lint, and repository inspection.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "integer", "minimum": 1, "maximum": 600},
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_diff",
            "description": "Read the current candidate diff and changed file list.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "load_skill",
            "description": "Load the full instructions for one advertised project skill.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_result",
            "description": "Request independent verification after implementing and testing the fix.",
            "parameters": {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
                "additionalProperties": False,
            },
        },
    },
]


class _ToolArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ReadFileArguments(_ToolArguments):
    path: str
    start_line: int = Field(default=1, ge=1)
    end_line: int | None = Field(default=None, ge=1)


class ListFilesArguments(_ToolArguments):
    path: str = "."
    limit: int = Field(default=300, ge=1, le=1000)


class SearchArguments(_ToolArguments):
    query: str
    path: str = "."
    limit: int = Field(default=100, ge=1, le=500)


class ReplaceInFileArguments(_ToolArguments):
    path: str
    old_text: str
    new_text: str


class WriteFileArguments(_ToolArguments):
    path: str
    content: str


class RunCommandArguments(_ToolArguments):
    command: str = Field(min_length=1)
    timeout: int = Field(default=120, ge=1, le=600)


class GitDiffArguments(_ToolArguments):
    pass


class LoadSkillArguments(_ToolArguments):
    name: str = Field(min_length=1)


class SubmitResultArguments(_ToolArguments):
    summary: str = Field(min_length=1)


TOOL_ARGUMENT_MODELS: dict[str, type[_ToolArguments]] = {
    "read_file": ReadFileArguments,
    "list_files": ListFilesArguments,
    "search": SearchArguments,
    "replace_in_file": ReplaceInFileArguments,
    "write_file": WriteFileArguments,
    "run_command": RunCommandArguments,
    "git_diff": GitDiffArguments,
    "load_skill": LoadSkillArguments,
    "submit_result": SubmitResultArguments,
}


def validate_tool_call(call: ToolCall) -> ToolCall:
    """Validate model arguments again at the trusted execution boundary."""
    arguments_model = TOOL_ARGUMENT_MODELS.get(call.name)
    if arguments_model is None:
        raise ValueError(f"Unknown tool: {call.name}")
    arguments = arguments_model.model_validate(call.arguments).model_dump(exclude_none=True)
    return call.model_copy(update={"arguments": arguments})


class ToolRegistry:
    def __init__(
        self,
        workspace: Path,
        executor: Executor,
        policy: PolicyEngine,
        store: SessionStore,
        session_id: str,
        skills: SkillLoader,
        hooks: HookManager,
        max_output_chars: int,
        base_commit: str = "HEAD",
    ):
        self.workspace = workspace.resolve()
        self.executor = executor
        self.policy = policy
        self.store = store
        self.session_id = session_id
        self.skills = skills
        self.hooks = hooks
        self.max_output_chars = max_output_chars
        self.base_commit = base_commit
        self.handlers: dict[str, Callable[[ToolCall], Awaitable[ToolResult]]] = {
            "read_file": self._read_file,
            "list_files": self._list_files,
            "search": self._search,
            "replace_in_file": self._replace,
            "write_file": self._write,
            "run_command": self._run_command,
            "git_diff": self._git_diff,
            "load_skill": self._load_skill,
        }

    @staticmethod
    def schemas() -> list[dict]:
        return TOOL_SCHEMAS

    async def execute(self, call: ToolCall) -> ToolResult:
        started = time.monotonic()
        self.store.start_tool(self.session_id, call.id, call.name, call.arguments)
        try:
            call = validate_tool_call(call)
            if call.name == "submit_result":
                raise ValueError("submit_result is controlled by the runtime")
            self.policy.check_tool(call.name, call.arguments)
            await self.hooks.before_tool(call.name, call.arguments)
            handler = self.handlers.get(call.name)
            if not handler:
                raise ValueError(f"Unknown tool: {call.name}")
            result = await handler(call)
            result = await self.hooks.after_tool(result)
        except PolicyViolation as exc:
            result = ToolResult(
                tool_call_id=call.id, name=call.name, status="denied", summary=str(exc)
            )
        except Exception as exc:
            result = ToolResult(
                tool_call_id=call.id,
                name=call.name,
                status="error",
                summary=f"{type(exc).__name__}: {exc}",
            )
        result.duration_ms = result.duration_ms or int((time.monotonic() - started) * 1000)
        result.changed_files = WorkspaceManager.changed_files(self.workspace, self.base_commit)
        self._offload(result)
        self.store.finish_tool(self.session_id, call.id, result.model_dump(mode="json"))
        return result

    def _offload(self, result: ToolResult) -> None:
        if len(result.output) <= self.max_output_chars:
            return
        artifact = self.store.write_artifact(
            self.session_id, f"tool-{result.tool_call_id}.log", result.output
        )
        head = result.output[: self.max_output_chars // 2]
        tail = result.output[-self.max_output_chars // 2 :]
        result.output = f"{head}\n\n... full output offloaded to {artifact} ...\n\n{tail}"
        result.artifact = artifact
        result.summary += "; output was truncated and archived"

    async def _read_file(self, call: ToolCall) -> ToolResult:
        path = self.policy.resolve_path(call.arguments["path"], must_exist=True)
        if not path.is_file():
            raise ValueError("Path is not a file")
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, int(call.arguments.get("start_line", 1)))
        end = min(len(lines), int(call.arguments.get("end_line", start + 399)))
        output = "\n".join(f"{number}: {lines[number - 1]}" for number in range(start, end + 1))
        return ToolResult(
            tool_call_id=call.id,
            name=call.name,
            status="ok",
            summary=f"Read {path.relative_to(self.workspace)} lines {start}-{end}",
            output=output,
        )

    async def _list_files(self, call: ToolCall) -> ToolResult:
        root = self.policy.resolve_path(call.arguments.get("path", "."), must_exist=True)
        limit = int(call.arguments.get("limit", 300))
        ignored = {".git", ".venv", "venv", "node_modules", "__pycache__", ".repofix"}
        rows = []
        for item in sorted(root.rglob("*")):
            if any(part in ignored for part in item.parts):
                continue
            if item.is_file():
                rows.append(item.relative_to(self.workspace).as_posix())
                if len(rows) >= limit:
                    rows.append("... truncated ...")
                    break
        return ToolResult(
            tool_call_id=call.id,
            name=call.name,
            status="ok",
            summary=f"Listed {len(rows)} entries",
            output="\n".join(rows),
        )

    async def _search(self, call: ToolCall) -> ToolResult:
        root = self.policy.resolve_path(call.arguments.get("path", "."), must_exist=True)
        pattern = re.compile(call.arguments["query"])
        limit = int(call.arguments.get("limit", 100))
        rows = []
        for file in sorted(root.rglob("*")):
            if not file.is_file() or ".git" in file.parts or file.stat().st_size > 2_000_000:
                continue
            try:
                file.resolve().relative_to(self.workspace)
            except ValueError:
                continue
            try:
                for number, line in enumerate(
                    file.read_text(encoding="utf-8", errors="strict").splitlines(), 1
                ):
                    if pattern.search(line):
                        rows.append(
                            f"{file.relative_to(self.workspace).as_posix()}:{number}: {line[:500]}"
                        )
                        if len(rows) >= limit:
                            break
            except (UnicodeDecodeError, OSError):
                continue
            if len(rows) >= limit:
                break
        return ToolResult(
            tool_call_id=call.id,
            name=call.name,
            status="ok",
            summary=f"Found {len(rows)} matches",
            output="\n".join(rows) or "No matches",
        )

    async def _replace(self, call: ToolCall) -> ToolResult:
        path = self.policy.resolve_path(call.arguments["path"], must_exist=True)
        old = call.arguments["old_text"]
        new = call.arguments["new_text"]
        content = path.read_text(encoding="utf-8")
        count = content.count(old)
        if count != 1:
            raise ValueError(f"old_text must occur exactly once; found {count}")
        path.write_text(content.replace(old, new, 1), encoding="utf-8", newline="")
        return ToolResult(
            tool_call_id=call.id,
            name=call.name,
            status="ok",
            summary=f"Updated {path.relative_to(self.workspace)}",
        )

    async def _write(self, call: ToolCall) -> ToolResult:
        path = self.policy.resolve_path(call.arguments["path"])
        if path.exists():
            raise ValueError("write_file refuses to overwrite; use replace_in_file")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(call.arguments["content"], encoding="utf-8", newline="")
        return ToolResult(
            tool_call_id=call.id,
            name=call.name,
            status="ok",
            summary=f"Created {path.relative_to(self.workspace)}",
        )

    async def _run_command(self, call: ToolCall) -> ToolResult:
        return await self.executor.run(
            call.arguments["command"],
            timeout=int(call.arguments.get("timeout", 120)),
            call_id=call.id,
        )

    async def _git_diff(self, call: ToolCall) -> ToolResult:
        changed = WorkspaceManager.changed_files(self.workspace, self.base_commit)
        diff = WorkspaceManager.candidate_patch(self.workspace, self.base_commit)
        untracked = []
        for rel in changed:
            try:
                path = self.policy.resolve_path(rel, must_exist=True)
            except PolicyViolation:
                continue
            if path.is_file() and rel not in diff and path.stat().st_size < 200_000:
                untracked.append(
                    f"\n--- untracked: {rel} ---\n{path.read_text(encoding='utf-8', errors='replace')}"
                )
        return ToolResult(
            tool_call_id=call.id,
            name=call.name,
            status="ok",
            summary=f"{len(changed)} changed files",
            output=diff + "".join(untracked),
            changed_files=changed,
        )

    async def _load_skill(self, call: ToolCall) -> ToolResult:
        content = self.skills.load(call.arguments["name"])
        return ToolResult(
            tool_call_id=call.id,
            name=call.name,
            status="ok",
            summary=f"Loaded skill {call.arguments['name']}",
            output=content,
        )


def tool_message(result: ToolResult) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": result.tool_call_id,
        "content": json.dumps(result.model_dump(mode="json"), ensure_ascii=False),
    }
