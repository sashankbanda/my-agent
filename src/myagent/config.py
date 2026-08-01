"""Typed application settings.

Settings are loaded from ``config/default.yaml`` and then overridden by
environment variables using the ``MYAGENT_`` prefix with ``__`` as the nesting
separator (``MYAGENT_LOGGING__LEVEL=DEBUG`` overrides ``logging.level``).

Invariant: this module holds *non-secret* configuration only. Secrets live in
the Windows Credential Manager and are referenced by name, never by value.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

ENV_PREFIX = "MYAGENT_"
ENV_NESTING_SEPARATOR = "__"


class AppSettings(BaseModel):
    """Identity and filesystem locations for the kernel."""

    name: str = "myagent"
    data_dir: Path | None = None

    def resolved_data_dir(self) -> Path:
        """Return the data directory, defaulting to %LOCALAPPDATA%/MyAgent."""
        if self.data_dir is not None:
            return self.data_dir
        local_app_data = os.environ.get("LOCALAPPDATA")
        base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
        return base / "MyAgent"


class LoggingSettings(BaseModel):
    """Log verbosity and output format."""

    level: str = "INFO"
    format: Literal["console", "json"] = "console"


class ServerSettings(BaseModel):
    """HTTP/WebSocket bind address for the FastAPI gateway."""

    host: str = "127.0.0.1"
    port: int = 8765


class VaultSettings(BaseModel):
    """Encrypted backup configuration.

    ``drive`` needs a Google OAuth client secrets file (one-time setup, see
    scripts/restore.py --help); ``folder`` needs only a directory path.
    """

    enabled: bool = False
    backend: Literal["drive", "folder"] = "drive"
    folder_name: str = "MyAgent Vault"  # Drive folder created on first upload
    local_path: Path | None = None  # folder backend target
    client_secrets: Path | None = None  # Google OAuth client json (drive backend)
    snapshot_hour: int = 3  # local hour of the daily snapshot
    keep_daily: int = 30
    keep_monthly: int = 12


class Settings(BaseModel):
    """Root of all kernel configuration."""

    app: AppSettings = Field(default_factory=AppSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    server: ServerSettings = Field(default_factory=ServerSettings)
    vault: VaultSettings = Field(default_factory=VaultSettings)
    features: dict[str, bool] = Field(default_factory=dict)

    def db_path(self) -> Path:
        """Absolute path of the operational SQLite database."""
        return self.app.resolved_data_dir() / "myagent.db"


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` into ``base`` and return the result."""
    merged = dict(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _env_overrides(environ: dict[str, str]) -> dict[str, Any]:
    """Build a nested override dict from MYAGENT_* environment variables.

    ``MYAGENT_LOGGING__LEVEL=DEBUG`` becomes ``{"logging": {"level": "DEBUG"}}``.
    Values are passed as strings; Pydantic coerces them to the field types.
    """
    overrides: dict[str, Any] = {}
    for name, value in environ.items():
        if not name.startswith(ENV_PREFIX):
            continue
        path = name.removeprefix(ENV_PREFIX).lower().split(ENV_NESTING_SEPARATOR)
        node = overrides
        for part in path[:-1]:
            node = node.setdefault(part, {})
        node[path[-1]] = value
    return overrides


def default_config_path() -> Path:
    """Path of the checked-in default configuration file."""
    return Path(__file__).resolve().parents[2] / "config" / "default.yaml"


def load_settings(config_path: Path | None = None) -> Settings:
    """Load settings from YAML, apply environment overrides, and validate.

    A missing config file is not an error: defaults are complete on their own,
    which keeps tests and fresh checkouts runnable.
    """
    path = config_path or default_config_path()
    raw: dict[str, Any] = {}
    if path.exists():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if loaded is not None:
            raw = loaded
    raw = _deep_merge(raw, _env_overrides(dict(os.environ)))
    return Settings.model_validate(raw)
