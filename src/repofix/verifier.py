from __future__ import annotations

import shutil
from pathlib import Path

from repofix.models import TaskSpec, ToolResult, VerificationResult
from repofix.policy import PolicyEngine
from repofix.sandbox import DockerExecutor, Executor, LocalExecutor
from repofix.store import SessionStore
from repofix.workspace import WorkspaceManager


class IndependentVerifier:
    """Applies the candidate patch to a clean clone and runs protected checks there."""

    def __init__(
        self,
        workspace: Path,
        task: TaskSpec,
        store: SessionStore,
        session_id: str,
        executor_kind: str,
        docker_image: str = "repofix-runner:py312",
        base_commit: str | None = None,
    ):
        self.workspace = workspace.resolve()
        self.task = task
        self.store = store
        self.session_id = session_id
        self.executor_kind = executor_kind
        self.docker_image = docker_image
        self.base_commit = base_commit or WorkspaceManager.base_commit(self.workspace)

    async def verify(self) -> VerificationResult:
        changed = WorkspaceManager.changed_files(self.workspace, self.base_commit)
        policy = PolicyEngine(self.workspace, self.task)
        violations = policy.check_changed_files(changed)
        patch = WorkspaceManager.candidate_patch(self.workspace, self.base_commit)
        patch_path = self.store.write_artifact(self.session_id, "candidate.patch", patch)
        if not changed:
            violations.append("No repository changes were produced")
        if violations:
            return VerificationResult(
                passed=False,
                summary="Candidate rejected by change policy",
                changed_files=changed,
                policy_violations=violations,
                patch_path=patch_path,
            )

        verify_root = self.store.paths.sessions / self.session_id / "verification"
        candidate = WorkspaceManager.make_verification_copy(
            self.workspace, verify_root, self.base_commit
        )
        if self.task.protected_tests:
            source = Path(self.task.protected_tests).resolve()
            if not source.exists():
                violations.append(f"Protected tests path does not exist: {source}")
            else:
                destination = candidate / ".repofix_hidden_tests"
                if source.is_dir():
                    shutil.copytree(source, destination, dirs_exist_ok=True)
                else:
                    destination.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination / source.name)
        if violations:
            return VerificationResult(
                passed=False,
                summary="Verifier setup failed",
                changed_files=changed,
                policy_violations=violations,
                patch_path=patch_path,
            )
        executor: Executor = (
            DockerExecutor(candidate, image=self.docker_image, network=self.task.allowed_network)
            if self.executor_kind == "docker"
            else LocalExecutor(candidate)
        )
        results: list[ToolResult] = []
        commands = list(self.task.acceptance_commands)
        if self.task.protected_tests:
            commands.append("python -m pytest -q .repofix_hidden_tests")
        for index, command in enumerate(commands, 1):
            result = await executor.run(command, timeout=600, call_id=f"verify-{index}")
            result.name = "verify_command"
            results.append(result)
            if result.status != "ok":
                break
        passed = bool(results) and all(result.status == "ok" for result in results)
        summary = "Independent verification passed" if passed else "Independent verification failed"
        return VerificationResult(
            passed=passed,
            summary=summary,
            command_results=results,
            changed_files=changed,
            policy_violations=violations,
            patch_path=patch_path,
        )
