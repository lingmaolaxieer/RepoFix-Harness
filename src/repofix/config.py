from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import BaseModel, Field


class ModelConfig(BaseModel):
    model: str = "gpt-4.1-mini"
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    timeout_seconds: int = Field(default=120, ge=5)
    max_retries: int = Field(default=2, ge=0, le=10)
    temperature: float = Field(default=0.0, ge=0, le=2)

    @classmethod
    def from_env(cls, model: str | None = None) -> "ModelConfig":
        return cls.from_sources(model=model)

    @classmethod
    def from_sources(cls, *, model: str | None = None, home: Path | None = None) -> "ModelConfig":
        values: dict[str, str] = {}
        candidates = [home / ".env" if home else None, Path.cwd() / ".env"]
        for candidate in candidates:
            if candidate and candidate.is_file():
                values.update(read_env_file(candidate))
        values.update({key: value for key, value in os.environ.items() if value is not None})
        return cls(
            model=model or values.get("REPOFIX_MODEL", "gpt-4.1-mini"),
            base_url=values.get("REPOFIX_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            api_key=values.get("REPOFIX_API_KEY", values.get("OPENAI_API_KEY", "")),
            timeout_seconds=int(values.get("REPOFIX_MODEL_TIMEOUT", "120")),
            max_retries=int(values.get("REPOFIX_MODEL_RETRIES", "2")),
        )


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value.startswith('"') and value.endswith('"'):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = value[1:-1]
        elif value.startswith("'") and value.endswith("'"):
            value = value[1:-1]
        values[key] = value
    return values


def write_model_env(path: Path, config: ModelConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    values = {
        "REPOFIX_API_KEY": config.api_key,
        "REPOFIX_BASE_URL": config.base_url,
        "REPOFIX_MODEL": config.model,
        "REPOFIX_MODEL_TIMEOUT": str(config.timeout_seconds),
        "REPOFIX_MODEL_RETRIES": str(config.max_retries),
    }
    content = "# Managed by RepoFix Web. This file contains secrets; do not commit it.\n"
    content += "\n".join(f"{key}={json.dumps(value)}" for key, value in values.items()) + "\n"
    path.write_text(content, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


class HarnessPaths(BaseModel):
    home: Path
    sessions: Path
    workspaces: Path
    sources: Path
    artifacts: Path
    database: Path

    @classmethod
    def under(cls, root: Path) -> "HarnessPaths":
        home = root.resolve()
        return cls(
            home=home,
            sessions=home / "sessions",
            workspaces=home / "workspaces",
            sources=home / "sources",
            artifacts=home / "artifacts",
            database=home / "repofix.sqlite3",
        )

    def ensure(self) -> None:
        for path in (self.home, self.sessions, self.workspaces, self.sources, self.artifacts):
            path.mkdir(parents=True, exist_ok=True)
