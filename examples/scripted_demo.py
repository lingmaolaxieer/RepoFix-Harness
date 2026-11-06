"""Offline end-to-end demonstration with a deterministic model and protected test."""

from __future__ import annotations

import asyncio
import subprocess
import tempfile
from pathlib import Path

from repofix.config import HarnessPaths
from repofix.model_client import ScriptedModel
from repofix.models import ModelResponse, TaskSpec, ToolCall
from repofix.runtime import AgentHarness
from repofix.store import SessionStore


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="repofix-demo-") as folder:
        root = Path(folder)
        repo = root / "buggy-repo"
        repo.mkdir()
        git(repo, "init", "-q")
        git(repo, "config", "user.email", "demo@repofix.local")
        git(repo, "config", "user.name", "RepoFix Demo")
        (repo / "calculator.py").write_text(
            "def mean(values):\n    return sum(values) / len(values)\n", encoding="utf-8"
        )
        tests = repo / "tests"
        tests.mkdir()
        (tests / "test_calculator.py").write_text(
            "from calculator import mean\n\ndef test_mean():\n    assert mean([2, 4]) == 3\n",
            encoding="utf-8",
        )
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "buggy baseline")

        protected = root / "protected"
        protected.mkdir()
        (protected / "test_empty.py").write_text(
            "from calculator import mean\n\ndef test_empty():\n    assert mean([]) is None\n",
            encoding="utf-8",
        )
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
                            id="test",
                            name="run_command",
                            arguments={"command": "python -m pytest -q"},
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
        store = SessionStore(HarnessPaths.under(root / "harness"))
        try:
            session = AgentHarness.create_session(
                store,
                repo,
                TaskSpec(
                    issue="mean([]) should return None",
                    acceptance_commands=["python -m pytest -q"],
                    protected_tests=str(protected),
                ),
                model="scripted",
                executor="local",
            )
            result = await AgentHarness(store, model).run(session.id)
            print(f"status={result.status.value}")
            print(f"verification={result.verification['summary']}")
            print(Path(result.verification["patch_path"]).read_text(encoding="utf-8"))
        finally:
            store.close()


if __name__ == "__main__":
    asyncio.run(main())
