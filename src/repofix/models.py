from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SessionStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"
    CLEANED = "cleaned"


class Budget(BaseModel):
    max_turns: int = Field(default=30, ge=1, le=200)
    max_tool_calls: int = Field(default=100, ge=1, le=1000)
    max_wall_seconds: int = Field(default=1800, ge=10, le=86400)
    max_context_chars: int = Field(default=80_000, ge=4_000)
    max_tool_output_chars: int = Field(default=8_000, ge=500)
    max_repeated_call: int = Field(default=3, ge=1, le=20)


class TaskSpec(BaseModel):
    issue: str = Field(min_length=1)
    acceptance_commands: list[str] = Field(default_factory=lambda: ["pytest -q"])
    forbidden_paths: list[str] = Field(
        default_factory=lambda: [".git", ".repofix", ".github/workflows"]
    )
    protected_tests: str | None = None
    max_changed_files: int = Field(default=25, ge=1, le=500)
    allowed_network: bool = False


class SessionRecord(BaseModel):
    id: str
    source_repo: str
    source_kind: Literal["git", "folder"] = "git"
    workspace_source: str | None = None
    workspace: str
    base_commit: str
    task: TaskSpec
    budget: Budget = Field(default_factory=Budget)
    model: str
    executor: Literal["docker", "local"] = "docker"
    status: SessionStatus = SessionStatus.CREATED
    turns: int = 0
    tool_calls: int = 0
    started_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)
    last_error: str | None = None
    verification: dict[str, Any] | None = None


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ModelResponse(BaseModel):
    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: dict[str, int] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict, exclude=True)


class ToolResult(BaseModel):
    tool_call_id: str
    name: str
    status: Literal["ok", "error", "denied", "timeout", "cancelled"]
    summary: str
    output: str = ""
    artifact: str | None = None
    exit_code: int | None = None
    duration_ms: int = 0
    changed_files: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class VerificationResult(BaseModel):
    passed: bool
    summary: str
    command_results: list[ToolResult] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)
    policy_violations: list[str] = Field(default_factory=list)
    patch_path: str | None = None


class Event(BaseModel):
    seq: int
    session_id: str
    kind: str
    payload: dict[str, Any]
    created_at: str = Field(default_factory=utc_now)
