"""Settings tests: YAML loading, env overrides, and defaults."""

from __future__ import annotations

from pathlib import Path

import pytest

from myagent.config import Settings, load_settings


def test_defaults_are_complete_without_config_file(tmp_path: Path) -> None:
    settings = load_settings(config_path=tmp_path / "missing.yaml")
    assert settings.app.name == "myagent"
    assert settings.server.port == 8765


def test_yaml_values_are_loaded(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("server:\n  port: 9000\n", encoding="utf-8")
    settings = load_settings(config_path=config)
    assert settings.server.port == 9000


def test_env_override_wins_over_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("logging:\n  level: INFO\nserver:\n  port: 9000\n", encoding="utf-8")
    monkeypatch.setenv("MYAGENT_LOGGING__LEVEL", "DEBUG")
    monkeypatch.setenv("MYAGENT_SERVER__PORT", "9100")
    settings = load_settings(config_path=config)
    assert settings.logging.level == "DEBUG"
    assert settings.server.port == 9100  # coerced from string by pydantic


def test_data_dir_defaults_under_localappdata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\test\AppData\Local")
    settings = Settings()
    resolved = settings.app.resolved_data_dir()
    assert resolved == Path(r"C:\Users\test\AppData\Local") / "MyAgent"
    assert settings.db_path().name == "myagent.db"


def test_explicit_data_dir_is_respected(tmp_path: Path) -> None:
    settings = Settings.model_validate({"app": {"data_dir": str(tmp_path)}})
    assert settings.app.resolved_data_dir() == tmp_path
