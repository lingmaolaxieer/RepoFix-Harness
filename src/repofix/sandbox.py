from __future__ import annotations

import asyncio
import os
import shlex
import signal
import time
import uuid
from abc import ABC, abstractmethod
from pathlib import Path

from repofix.models import ToolResult


class Executor(ABC):
    @abstractmethod
    async def run(
        self, command: str, *, timeout: int = 120, call_id: str = "command"
    ) -> ToolResult: ...


async def _terminate(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        if os.name == "nt":
            process.terminate()
        else:
            os.killpg(process.pid, signal.SIGTERM)
        await asyncio.wait_for(process.wait(), timeout=3)
    except (ProcessLookupError, asyncio.TimeoutError):
        try:
            if os.name == "nt":
                process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


class LocalExecutor(Executor):
    """Executor for tests and explicitly trusted repositories."""

    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()

    async def run(
        self, command: str, *, timeout: int = 120, call_id: str = "command"
    ) -> ToolResult:
        started = time.monotonic()
        kwargs = {
            "cwd": str(self.workspace),
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.STDOUT,
        }
        if os.name == "nt":
            process = await asyncio.create_subprocess_exec(
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                command,
                creationflags=0x00000200,
                **kwargs,
            )
        else:
            process = await asyncio.create_subprocess_exec(
                "/bin/sh", "-lc", command, start_new_session=True, **kwargs
            )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
            status = "ok" if process.returncode == 0 else "error"
        except asyncio.TimeoutError:
            await _terminate(process)
            output = b"Command timed out and its process was terminated."
            status = "timeout"
        except asyncio.CancelledError:
            await _terminate(process)
            raise
        text = output.decode("utf-8", errors="replace")
        return ToolResult(
            tool_call_id=call_id,
            name="run_command",
            status=status,
            summary=f"exit={process.returncode} ({status})",
            output=text,
            exit_code=process.returncode,
            duration_ms=int((time.monotonic() - started) * 1000),
        )


class DockerExecutor(Executor):
    """Runs each command in a short-lived, network-disabled Linux container."""

    def __init__(
        self,
        workspace: Path,
        *,
        image: str = "repofix-runner:py312",
        memory: str = "1g",
        cpus: float = 1.5,
        pids: int = 256,
        network: bool = False,
    ):
        self.workspace = workspace.resolve()
        self.image = image
        self.memory = memory
        self.cpus = cpus
        self.pids = pids
        self.network = network

    async def run(
        self, command: str, *, timeout: int = 120, call_id: str = "command"
    ) -> ToolResult:
        started = time.monotonic()
        name = f"repofix-{uuid.uuid4().hex[:12]}"
        if os.name == "nt":
            container_user = "1000:1000"
        else:
            host_uid = os.getuid()
            host_gid = os.getgid()
            container_user = f"{host_uid}:{host_gid}" if host_uid else "1000:1000"
        args = [
            "docker",
            "run",
            "--rm",
            "--name",
            name,
            "--user",
            container_user,
            "--workdir",
            "/workspace",
            "--mount",
            f"type=bind,source={self.workspace},target=/workspace",
            "--memory",
            self.memory,
            "--cpus",
            str(self.cpus),
            "--pids-limit",
            str(self.pids),
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--read-only",
            "--env",
            "HOME=/tmp",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=256m",
        ]
        if not self.network:
            args.extend(["--network", "none"])
        args.extend([self.image, "/bin/sh", "-lc", command])
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            creationflags=0x00000200 if os.name == "nt" else 0,
            **({} if os.name == "nt" else {"start_new_session": True}),
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
            status = "ok" if process.returncode == 0 else "error"
        except asyncio.TimeoutError:
            killer = await asyncio.create_subprocess_exec(
                "docker",
                "rm",
                "-f",
                name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
            await _terminate(process)
            output = b"Container command timed out; the container was forcibly removed."
            status = "timeout"
        except asyncio.CancelledError:
            killer = await asyncio.create_subprocess_exec(
                "docker",
                "rm",
                "-f",
                name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
            await _terminate(process)
            raise
        text = output.decode("utf-8", errors="replace")
        return ToolResult(
            tool_call_id=call_id,
            name="run_command",
            status=status,
            summary=f"container exit={process.returncode} ({status})",
            output=text,
            exit_code=process.returncode,
            duration_ms=int((time.monotonic() - started) * 1000),
            metadata={"image": self.image, "network": self.network, "container": name},
        )


def shell_join(parts: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in parts)
