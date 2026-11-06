from __future__ import annotations

from pathlib import Path

from repofix.config import HarnessPaths, ModelConfig
from repofix.web import WebApplication


def test_web_settings_are_persistent_and_secret_is_masked(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    app = WebApplication(HarnessPaths.under(tmp_path / "home"))

    view = app.save_config(
        {
            "api_key": "secret-value-1234",
            "base_url": "https://provider.example/v1/",
            "model": "coding-model",
            "timeout_seconds": 45,
            "max_retries": 4,
        }
    )

    assert view["has_api_key"] is True
    assert view["api_key_hint"] == "••••1234"
    assert "secret-value" not in str(view)
    loaded = ModelConfig.from_sources(home=app.paths.home)
    assert loaded.api_key == "secret-value-1234"
    assert loaded.base_url == "https://provider.example/v1"
    assert loaded.model == "coding-model"


def test_web_inspects_git_and_plain_folders(tmp_path: Path):
    app = WebApplication(HarnessPaths.under(tmp_path / "home"))
    plain = tmp_path / "plain"
    plain.mkdir()

    result = app.inspect_project({"path": str(plain)})

    assert result["source_kind"] == "folder"
    assert result["ready"] is True
    assert "原目录不会被修改" in result["message"]
