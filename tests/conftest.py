from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from repofix.config import HarnessPaths
from repofix.store import SessionStore


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], text=True, capture_output=True, check=True
    )
    return result.stdout.strip()


@pytest.fixture
def source_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "tests@repofix.local")
    git(repo, "config", "user.name", "RepoFix Tests")
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
    git(repo, "commit", "-qm", "fixture")
    return repo


@pytest.fixture
def store(tmp_path: Path):
    value = SessionStore(HarnessPaths.under(tmp_path / "harness"))
    yield value
    value.close()
