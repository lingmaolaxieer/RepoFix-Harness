from __future__ import annotations

import fnmatch
from pathlib import Path, PurePosixPath
from typing import Any

from repofix.models import TaskSpec


class PolicyViolation(ValueError):
    pass


class PolicyEngine:
    def __init__(self, workspace: Path, task: TaskSpec):
        self.workspace = workspace.resolve()
        self.task = task

    def resolve_path(self, raw: str, *, must_exist: bool = False) -> Path:
        candidate = (self.workspace / raw).resolve()
        try:
            rel = candidate.relative_to(self.workspace).as_posix()
        except ValueError as exc:
            raise PolicyViolation("Path escapes the task workspace") from exc
        if rel == ".":
            rel = ""
        for pattern in self.task.forbidden_paths:
            normalized = pattern.replace("\\", "/").rstrip("/")
            if (
                rel == normalized
                or rel.startswith(normalized + "/")
                or fnmatch.fnmatch(rel, normalized)
            ):
                raise PolicyViolation(f"Access denied by path policy: {rel or '.'}")
        if must_exist and not candidate.exists():
            raise PolicyViolation(f"Path does not exist: {raw}")
        return candidate

    def check_tool(self, name: str, arguments: dict[str, Any]) -> None:
        if name in {"read_file", "replace_in_file", "write_file", "list_files"}:
            self.resolve_path(
                str(arguments.get("path", ".")), must_exist=name in {"read_file", "replace_in_file"}
            )
        if name == "search" and arguments.get("path"):
            self.resolve_path(str(arguments["path"]), must_exist=True)

    def check_changed_files(self, changed: list[str]) -> list[str]:
        violations = []
        if len(changed) > self.task.max_changed_files:
            violations.append(
                f"Changed {len(changed)} files; maximum is {self.task.max_changed_files}"
            )
        for item in changed:
            posix = PurePosixPath(item).as_posix()
            for forbidden in self.task.forbidden_paths:
                normalized = forbidden.replace("\\", "/").rstrip("/")
                if posix == normalized or posix.startswith(normalized + "/"):
                    violations.append(f"Forbidden path changed: {posix}")
        return violations
