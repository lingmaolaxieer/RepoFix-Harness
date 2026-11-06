from __future__ import annotations

from pathlib import Path

from repofix.hooks import HookManager
from repofix.models import Budget, SessionRecord, TaskSpec, ToolCall
from repofix.policy import PolicyEngine
from repofix.sandbox import LocalExecutor
from repofix.skills import SkillLoader
from repofix.tools import ToolRegistry
from repofix.workspace import WorkspaceManager


async def test_exact_edit_and_large_output_offload(store, source_repo: Path):
    identifier, workspace, base = WorkspaceManager(store.paths).create(source_repo)
    task = TaskSpec(issue="fix")
    store.create(
        SessionRecord(
            id=identifier,
            source_repo=str(source_repo),
            workspace=str(workspace),
            base_commit=base,
            task=task,
            budget=Budget(),
            model="scripted",
            executor="local",
        )
    )
    registry = ToolRegistry(
        workspace,
        LocalExecutor(workspace),
        PolicyEngine(workspace, task),
        store,
        identifier,
        SkillLoader([]),
        HookManager(),
        max_output_chars=500,
    )
    edit = await registry.execute(
        ToolCall(
            id="edit",
            name="replace_in_file",
            arguments={
                "path": "calculator.py",
                "old_text": "return sum(values) / len(values)",
                "new_text": "return None if not values else sum(values) / len(values)",
            },
        )
    )
    assert edit.status == "ok"
    result = await registry.execute(
        ToolCall(
            id="command",
            name="run_command",
            arguments={"command": "python -c \"print('x' * 2000)\""},
        )
    )
    assert result.status == "ok"
    assert result.artifact and Path(result.artifact).exists()
    assert len(result.output) < 1000


async def test_duplicate_exact_match_is_rejected(store, source_repo: Path):
    identifier, workspace, base = WorkspaceManager(store.paths).create(source_repo)
    path = workspace / "dupe.txt"
    path.write_text("same\nsame\n", encoding="utf-8")
    task = TaskSpec(issue="fix")
    store.create(
        SessionRecord(
            id=identifier,
            source_repo=str(source_repo),
            workspace=str(workspace),
            base_commit=base,
            task=task,
            model="scripted",
            executor="local",
        )
    )
    registry = ToolRegistry(
        workspace,
        LocalExecutor(workspace),
        PolicyEngine(workspace, task),
        store,
        identifier,
        SkillLoader([]),
        HookManager(),
        max_output_chars=500,
    )
    result = await registry.execute(
        ToolCall(
            id="edit",
            name="replace_in_file",
            arguments={"path": "dupe.txt", "old_text": "same", "new_text": "other"},
        )
    )
    assert result.status == "error"
    assert "exactly once" in result.summary


async def test_tool_arguments_are_strictly_validated_before_execution(store, source_repo: Path):
    identifier, workspace, base = WorkspaceManager(store.paths).create(source_repo)
    task = TaskSpec(issue="validate tools")
    store.create(
        SessionRecord(
            id=identifier,
            source_repo=str(source_repo),
            workspace=str(workspace),
            base_commit=base,
            task=task,
            model="scripted",
            executor="local",
        )
    )
    registry = ToolRegistry(
        workspace,
        LocalExecutor(workspace),
        PolicyEngine(workspace, task),
        store,
        identifier,
        SkillLoader([]),
        HookManager(),
        max_output_chars=500,
        base_commit=base,
    )

    wrong_type = await registry.execute(
        ToolCall(
            id="wrong-type",
            name="run_command",
            arguments={"command": "python -c \"print('must not run')\"", "timeout": "5"},
        )
    )
    extra_field = await registry.execute(
        ToolCall(
            id="extra-field",
            name="read_file",
            arguments={"path": "calculator.py", "unexpected": True},
        )
    )

    assert wrong_type.status == "error"
    assert "ValidationError" in wrong_type.summary
    assert extra_field.status == "error"
    assert "extra_forbidden" in extra_field.summary
