from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import uuid
from pathlib import Path

from repofix.config import HarnessPaths


class WorkspaceError(RuntimeError):
    pass


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], text=True, capture_output=True, timeout=60
    )
    if check and result.returncode:
        raise WorkspaceError(result.stderr.strip() or result.stdout.strip())
    return result


class WorkspaceManager:
    def __init__(self, paths: HarnessPaths):
        self.paths = paths

    def create(self, source_repo: Path, session_id: str | None = None) -> tuple[str, Path, str]:
        source = source_repo.resolve()
        if not source.is_dir():
            raise WorkspaceError(f"Project folder does not exist: {source}")
        identifier = session_id or uuid.uuid4().hex[:12]
        if self.is_git_repository(source):
            source = Path(_git(source, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
            if _git(source, "status", "--porcelain").stdout.strip():
                raise WorkspaceError("Git project must be clean before creating a task workspace")
        else:
            source = self._snapshot_folder(source, identifier)
        target = (self.paths.workspaces / identifier).resolve()
        if target.exists():
            raise WorkspaceError(f"Workspace already exists: {target}")
        base = _git(source, "rev-parse", "HEAD").stdout.strip()
        result = subprocess.run(
            ["git", "-C", str(source), "worktree", "add", "--detach", str(target), base],
            text=True,
            capture_output=True,
            timeout=120,
        )
        if result.returncode:
            raise WorkspaceError(result.stderr.strip() or result.stdout.strip())
        return identifier, target, base

    @staticmethod
    def is_git_repository(source: Path) -> bool:
        return (
            _git(source.resolve(), "rev-parse", "--is-inside-work-tree", check=False).returncode
            == 0
        )

    def _snapshot_folder(self, source: Path, identifier: str) -> Path:
        baseline = (self.paths.sources / identifier).resolve()
        if self.paths.home.resolve().is_relative_to(source):
            raise WorkspaceError("Project folder cannot contain the RepoFix controller directory")
        if baseline.exists():
            raise WorkspaceError(f"Managed source snapshot already exists: {baseline}")
        try:
            shutil.copytree(source, baseline, ignore=self._snapshot_ignore)
            _git(baseline, "init", "-q")
            _git(baseline, "config", "user.email", "repofix@local")
            _git(baseline, "config", "user.name", "RepoFix Snapshot")
            _git(baseline, "add", "-A")
            _git(baseline, "commit", "--allow-empty", "-qm", "RepoFix source snapshot")
        except Exception:
            if baseline.exists():
                self.delete_managed_source(baseline, self.paths.sources)
            raise
        return baseline

    @staticmethod
    def _snapshot_ignore(_directory: str, names: list[str]) -> set[str]:
        ignored_names = {
            ".git",
            ".repofix",
            ".venv",
            "venv",
            "node_modules",
            "__pycache__",
            ".pytest_cache",
            ".ruff_cache",
            ".mypy_cache",
            "build",
            "dist",
        }
        ignored = {name for name in names if name in ignored_names}
        ignored.update(
            name
            for name in names
            if (name == ".env" or name.startswith(".env.")) and name != ".env.example"
        )
        return ignored

    @staticmethod
    def source_repository(workspace: Path) -> Path:
        raw = _git(workspace, "rev-parse", "--git-common-dir").stdout.strip()
        common = Path(raw)
        if not common.is_absolute():
            common = (workspace / common).resolve()
        return common.parent if common.name == ".git" else common

    @staticmethod
    def diff(workspace: Path, *, binary: bool = False) -> str:
        args = ["diff", "--no-ext-diff"]
        if binary:
            args.append("--binary")
        return _git(workspace, *args).stdout

    @staticmethod
    def candidate_patch(workspace: Path, base_commit: str = "HEAD") -> str:
        changed = WorkspaceManager.changed_files(workspace, base_commit)
        if not changed:
            return ""
        descriptor, index_name = tempfile.mkstemp(prefix="repofix-index-")
        os.close(descriptor)
        Path(index_name).unlink()
        environment = os.environ.copy()
        environment["GIT_INDEX_FILE"] = index_name
        try:
            WorkspaceManager._git_with_env(workspace, environment, "read-tree", base_commit)
            WorkspaceManager._git_with_env(workspace, environment, "add", "-A", "--", *changed)
            return WorkspaceManager._git_with_env(
                workspace, environment, "diff", "--cached", "--binary", base_commit
            ).stdout
        finally:
            Path(index_name).unlink(missing_ok=True)

    @staticmethod
    def changed_files(workspace: Path, base_commit: str = "HEAD") -> list[str]:
        # Comparing against the task baseline includes staged, unstaged, and committed
        # changes made by the agent. Untracked files are added separately below.
        tracked = _git(workspace, "diff", "--name-only", base_commit).stdout.splitlines()
        untracked = _git(
            workspace, "ls-files", "--others", "--exclude-standard"
        ).stdout.splitlines()
        untracked = [item for item in untracked if not WorkspaceManager._transient(item)]
        return sorted(set(filter(None, tracked + untracked)))

    @staticmethod
    def _transient(relative: str) -> bool:
        path = Path(relative)
        ignored_parts = {"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
        return bool(
            ignored_parts.intersection(path.parts)
            or path.suffix in {".pyc", ".pyo"}
            or path.name in {".coverage", "coverage.xml"}
        )

    @staticmethod
    def _git_with_env(
        repo: Path, environment: dict[str, str], *args: str
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=False,
            text=True,
            capture_output=True,
            timeout=60,
            env=environment,
        )
        if result.returncode:
            raise WorkspaceError(result.stderr.strip() or result.stdout.strip())
        return result

    @staticmethod
    def base_commit(workspace: Path) -> str:
        return _git(workspace, "rev-parse", "HEAD").stdout.strip()

    @staticmethod
    def make_verification_copy(workspace: Path, target: Path, base_commit: str = "HEAD") -> Path:
        if target.exists():
            shutil.rmtree(target)
        result = subprocess.run(
            ["git", "clone", "--quiet", "--no-hardlinks", str(workspace), str(target)],
            text=True,
            capture_output=True,
            timeout=120,
        )
        if result.returncode:
            raise WorkspaceError(result.stderr.strip())
        checkout = _git(target, "checkout", "--detach", base_commit, check=False)
        if checkout.returncode:
            raise WorkspaceError(checkout.stderr.strip() or checkout.stdout.strip())
        patch = WorkspaceManager.candidate_patch(workspace, base_commit)
        if patch:
            apply = subprocess.run(
                ["git", "-C", str(target), "apply", "--binary", "-"],
                input=patch,
                text=True,
                capture_output=True,
                timeout=60,
            )
            if apply.returncode:
                raise WorkspaceError(f"Candidate patch does not apply cleanly: {apply.stderr}")
        return target

    @staticmethod
    def remove(source_repo: Path, workspace: Path) -> None:
        source = source_repo.resolve()
        target = workspace.resolve()
        if target == source or target.parent == target:
            raise WorkspaceError("Refusing to remove an unsafe workspace target")
        result = subprocess.run(
            ["git", "-C", str(source), "worktree", "remove", "--force", str(target)],
            check=False,
            text=True,
            capture_output=True,
            timeout=120,
        )
        if result.returncode:
            raise WorkspaceError(result.stderr.strip() or result.stdout.strip())

    @staticmethod
    def delete_managed_source(source_repo: Path, sources_root: Path) -> None:
        source = source_repo.resolve()
        root = sources_root.resolve()
        if source.parent != root or source == root:
            raise WorkspaceError(f"Refusing to remove source outside managed snapshots: {source}")
        if not source.exists():
            return

        def make_writable(function, path, _error_info):
            os.chmod(path, stat.S_IWRITE)
            function(path)

        shutil.rmtree(source, onerror=make_writable)
