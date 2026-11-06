from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from repofix.sandbox import DockerExecutor


def _runner_exists() -> bool:
    return (
        subprocess.run(
            ["docker", "image", "inspect", "repofix-runner:py312"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    )


@pytest.mark.docker
@pytest.mark.skipif(not _runner_exists(), reason="build repofix-runner:py312 first")
async def test_docker_executor_writes_workspace_and_has_no_network(tmp_path: Path):
    executor = DockerExecutor(tmp_path)
    result = await executor.run(
        "python -c \"from pathlib import Path; Path('ok.txt').write_text('ok')\""
    )
    assert result.status == "ok"
    assert (tmp_path / "ok.txt").read_text() == "ok"
    identity = await executor.run('test "$(id -u)" -ne 0')
    assert identity.status == "ok"
    network = await executor.run(
        "python -c \"import socket; socket.create_connection(('1.1.1.1', 53), timeout=1)\"",
        timeout=10,
    )
    assert network.status == "error"
