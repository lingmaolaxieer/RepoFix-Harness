"""Deterministic end-to-end evaluation of the RepoFix harness.

This evaluates orchestration, editing, verification, protected tests, and patch
delivery with a scripted model. It deliberately does not claim real-model quality.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import platform
import subprocess
import sys
import tempfile
from pathlib import Path

from repofix.config import HarnessPaths
from repofix.evaluation import run_evaluation
from repofix.model_client import ScriptedModel
from repofix.models import ModelResponse, ToolCall
from repofix.store import SessionStore


def git(repo: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(repo), *arguments], check=True, capture_output=True, text=True)


def create_repository(root: Path, name: str, source: str, test_source: str) -> Path:
    repository = root / name
    repository.mkdir()
    git(repository, "init", "-q")
    git(repository, "config", "user.email", "evaluation@repofix.local")
    git(repository, "config", "user.name", "RepoFix Evaluation")
    (repository / "app.py").write_text(source, encoding="utf-8")
    (repository / "test_app.py").write_text(test_source, encoding="utf-8")
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "evaluation baseline")
    return repository


def scripted_model() -> ScriptedModel:
    def call(identifier: str, name: str, arguments: dict) -> ModelResponse:
        return ModelResponse(tool_calls=[ToolCall(id=identifier, name=name, arguments=arguments)])

    return ScriptedModel(
        [
            call("mean-read", "read_file", {"path": "app.py"}),
            call(
                "mean-edit",
                "replace_in_file",
                {
                    "path": "app.py",
                    "old_text": "return sum(values) / len(values)",
                    "new_text": "return None if not values else sum(values) / len(values)",
                },
            ),
            call("mean-test", "run_command", {"command": "python -m pytest -q"}),
            call("mean-submit", "submit_result", {"summary": "Handle empty input"}),
            call("hello-read", "read_file", {"path": "app.py"}),
            call(
                "hello-edit",
                "replace_in_file",
                {
                    "path": "app.py",
                    "old_text": 'return f"Hello, {name}"',
                    "new_text": "return f\"Hello, {name or 'stranger'}\"",
                },
            ),
            call("hello-test", "run_command", {"command": "python -m pytest -q"}),
            call("hello-submit", "submit_result", {"summary": "Handle an empty name"}),
        ]
    )


async def evaluate(output: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="repofix-offline-eval-") as temporary:
        root = Path(temporary)
        mean_repo = create_repository(
            root,
            "mean",
            "def mean(values):\n    return sum(values) / len(values)\n",
            "from app import mean\n\ndef test_mean():\n    assert mean([2, 4]) == 3\n",
        )
        hello_repo = create_repository(
            root,
            "hello",
            'def greeting(name):\n    return f"Hello, {name}"\n',
            'from app import greeting\n\ndef test_greeting():\n    assert greeting("Ada") == "Hello, Ada"\n',
        )
        mean_hidden = root / "mean-hidden"
        mean_hidden.mkdir()
        (mean_hidden / "test_empty.py").write_text(
            "from app import mean\n\ndef test_empty():\n    assert mean([]) is None\n",
            encoding="utf-8",
        )
        hello_hidden = root / "hello-hidden"
        hello_hidden.mkdir()
        (hello_hidden / "test_empty.py").write_text(
            "from app import greeting\n\ndef test_empty():\n"
            '    assert greeting("") == "Hello, stranger"\n',
            encoding="utf-8",
        )
        manifest = root / "manifest.json"
        manifest.write_text(
            json.dumps(
                [
                    {
                        "id": "empty-mean",
                        "repository": str(mean_repo),
                        "task": {
                            "issue": "mean([]) should return None",
                            "acceptance_commands": ["python -m pytest -q"],
                            "protected_tests": str(mean_hidden),
                        },
                        "tags": ["boundary-condition", "protected-test"],
                    },
                    {
                        "id": "empty-greeting",
                        "repository": str(hello_repo),
                        "task": {
                            "issue": "greeting('') should return Hello, stranger",
                            "acceptance_commands": ["python -m pytest -q"],
                            "protected_tests": str(hello_hidden),
                        },
                        "tags": ["boundary-condition", "protected-test"],
                    },
                ],
                indent=2,
            ),
            encoding="utf-8",
        )
        store = SessionStore(HarnessPaths.under(root / "harness"))
        try:
            report = await run_evaluation(
                manifest,
                store,
                scripted_model(),
                model_name="scripted-offline",
                executor="local",
            )
        finally:
            store.close()

    report_payload = report.model_dump(mode="json")
    for case in report_payload["cases"]:
        case.pop("session_id", None)
    payload = {
        "generated_date": datetime.date.today().isoformat(),
        "scope": "deterministic harness evaluation; not a real-model quality benchmark",
        "python": sys.version.split()[0],
        "platform": f"{platform.system()} {platform.machine()}",
        "usage_note": "ScriptedModel does not report tokens; zero usage is not a cost claim.",
        "report": report_payload,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(output.resolve())
    if report.passed != report.total:
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output", type=Path, default=Path("docs/evaluation-results/offline-latest.json")
    )
    arguments = parser.parse_args()
    asyncio.run(evaluate(arguments.output))


if __name__ == "__main__":
    main()
