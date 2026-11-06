from __future__ import annotations

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from repofix.models import Budget, SessionRecord, TaskSpec
from repofix.store import SessionStore
from repofix.workspace import WorkspaceManager


def test_worktree_is_isolated_and_patch_applies(store, source_repo: Path):
    identifier, workspace, base = WorkspaceManager(store.paths).create(source_repo)
    assert base
    target = workspace / "calculator.py"
    target.write_text(target.read_text(encoding="utf-8") + "\n# candidate\n", encoding="utf-8")
    (workspace / "new_file.py").write_text("NEW = True\n", encoding="utf-8")
    assert "candidate" not in (source_repo / "calculator.py").read_text(encoding="utf-8")
    assert WorkspaceManager.changed_files(workspace) == ["calculator.py", "new_file.py"]
    assert "new_file.py" in WorkspaceManager.candidate_patch(workspace)
    copy = WorkspaceManager.make_verification_copy(workspace, store.paths.home / "verify")
    assert "candidate" in (copy / "calculator.py").read_text(encoding="utf-8")
    assert (copy / "new_file.py").read_text(encoding="utf-8") == "NEW = True\n"


def test_incomplete_tool_is_recoverable_without_replay(store, source_repo: Path):
    identifier, workspace, base = WorkspaceManager(store.paths).create(source_repo)
    record = SessionRecord(
        id=identifier,
        source_repo=str(source_repo),
        workspace=str(workspace),
        base_commit=base,
        task=TaskSpec(issue="fix"),
        budget=Budget(),
        model="scripted",
        executor="local",
    )
    store.create(record)
    store.start_tool(identifier, "call-1", "run_command", {"command": "touch marker"})
    pending = store.indeterminate_tools(identifier)
    assert pending == [
        {
            "id": "call-1",
            "name": "run_command",
            "arguments": {"command": "touch marker"},
            "started_at": pending[0]["started_at"],
        }
    ]
    store.mark_tool_indeterminate(identifier, "call-1")
    assert store.indeterminate_tools(identifier) == []


def test_workspace_cleanup_preserves_source_repository(store, source_repo: Path):
    _, workspace, _ = WorkspaceManager(store.paths).create(source_repo)
    assert workspace.exists()
    WorkspaceManager.remove(source_repo, workspace)
    assert not workspace.exists()
    assert (source_repo / "calculator.py").exists()


def test_plain_folder_is_snapshotted_without_modifying_original(store, tmp_path: Path):
    source = tmp_path / "plain-project"
    source.mkdir()
    (source / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (source / ".env").write_text("SECRET=do-not-copy\n", encoding="utf-8")
    (source / ".venv").mkdir()
    (source / ".venv" / "ignored.txt").write_text("large dependency", encoding="utf-8")

    _, workspace, _ = WorkspaceManager(store.paths).create(source)

    assert (workspace / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert not (workspace / ".env").exists()
    assert not (workspace / ".venv").exists()
    (workspace / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    assert (source / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert "VALUE = 2" in WorkspaceManager.candidate_patch(workspace)

    managed_source = WorkspaceManager.source_repository(workspace)
    assert managed_source.parent == store.paths.sources
    WorkspaceManager.remove(managed_source, workspace)
    WorkspaceManager.delete_managed_source(managed_source, store.paths.sources)
    assert (source / "app.py").exists()


def test_patch_is_bound_to_original_base_even_after_agent_commit(store, source_repo: Path):
    _, workspace, base = WorkspaceManager(store.paths).create(source_repo)
    target = workspace / "calculator.py"
    target.write_text(
        target.read_text(encoding="utf-8").replace(
            "return sum(values) / len(values)",
            "return None if not values else sum(values) / len(values)",
        ),
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "-C", str(workspace), "add", "calculator.py"], check=True, capture_output=True
    )
    assert WorkspaceManager.changed_files(workspace, base) == ["calculator.py"]
    subprocess.run(
        ["git", "-C", str(workspace), "commit", "-qm", "agent commit"],
        check=True,
        capture_output=True,
    )

    assert WorkspaceManager.changed_files(workspace, base) == ["calculator.py"]
    patch = WorkspaceManager.candidate_patch(workspace, base)
    assert "return None if not values" in patch
    copy = WorkspaceManager.make_verification_copy(
        workspace, store.paths.home / "verify-agent-commit", base
    )
    assert "return None if not values" in (copy / "calculator.py").read_text(encoding="utf-8")


def test_event_sequences_are_serialized_across_store_connections(store, source_repo: Path):
    identifier, workspace, base = WorkspaceManager(store.paths).create(source_repo)
    store.create(
        SessionRecord(
            id=identifier,
            source_repo=str(source_repo),
            workspace=str(workspace),
            base_commit=base,
            task=TaskSpec(issue="concurrent trace"),
            model="scripted",
            executor="local",
        )
    )
    second = SessionStore(store.paths)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(target.append_event, identifier, "concurrent", {"index": index})
                for index, target in enumerate([store, second] * 10)
            ]
            for future in futures:
                future.result(timeout=10)
    finally:
        second.close()

    sequences = [event.seq for event in store.events(identifier)]
    assert sequences == list(range(1, len(sequences) + 1))
    trace_path = store.paths.sessions / identifier / "events.jsonl"
    trace_sequences = [json.loads(line)["seq"] for line in trace_path.read_text().splitlines()]
    assert trace_sequences == sequences
