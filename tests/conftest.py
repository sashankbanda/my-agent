"""Shared fixtures: isolated per-test database and an in-memory keyring.

The keyring isolation is mandatory: tests must never read or write the real
Windows Credential Manager (they would leak state between runs - or worse,
create a real vault key on the developer's machine).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import keyring
import keyring.backend
import pytest

from myagent.config import Settings
from myagent.db import connect, migrate


class InMemoryKeyring(keyring.backend.KeyringBackend):
    """Volatile keyring for tests."""

    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.store.pop((service, username), None)


@pytest.fixture(autouse=True)
def isolated_keyring() -> Iterator[InMemoryKeyring]:
    """Every test runs against a fresh, in-memory credential store."""
    previous = keyring.get_keyring()
    memory_keyring = InMemoryKeyring()
    keyring.set_keyring(memory_keyring)
    try:
        yield memory_keyring
    finally:
        keyring.set_keyring(previous)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings pointing at a per-test data directory."""
    return Settings.model_validate({"app": {"data_dir": str(tmp_path / "data")}})


@pytest.fixture
def db(settings: Settings) -> Iterator[sqlite3.Connection]:
    """A migrated connection to the per-test database."""
    conn = connect(settings.db_path())
    migrate(conn)
    try:
        yield conn
    finally:
        conn.close()
