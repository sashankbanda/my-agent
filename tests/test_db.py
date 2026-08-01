"""Foundation tests: database pragmas, migrations, and the event log."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from myagent.config import Settings
from myagent.db import connect, migrate, transaction
from myagent.events import EventType, append_event, read_event


def test_connection_uses_wal(settings: Settings) -> None:
    conn = connect(settings.db_path())
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"
    finally:
        conn.close()


def test_migrate_is_idempotent(settings: Settings) -> None:
    conn = connect(settings.db_path())
    try:
        first = migrate(conn)
        second = migrate(conn)
        assert first == list(range(1, len(first) + 1))  # contiguous, ordered, all applied
        assert second == []  # re-running applies nothing
        versions = sorted(
            row["version"] for row in conn.execute("SELECT version FROM schema_version")
        )
        assert versions == first
    finally:
        conn.close()


def test_failed_migration_rolls_back(settings: Settings, tmp_path: Path) -> None:
    """A broken migration leaves the schema at the previous version."""
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0001_good.sql").write_text("CREATE TABLE t1 (x INTEGER);", encoding="utf-8")
    (migrations / "0002_bad.sql").write_text(
        "CREATE TABLE t2 (y INTEGER); THIS IS NOT SQL;", encoding="utf-8"
    )
    conn = connect(settings.db_path())
    try:
        with pytest.raises(sqlite3.OperationalError):
            migrate(conn, migrations)
        versions = [row["version"] for row in conn.execute("SELECT version FROM schema_version")]
        assert versions == [1]
        tables = {
            row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "t1" in tables
        assert "t2" not in tables
    finally:
        conn.close()


def test_append_event_round_trips(db: sqlite3.Connection) -> None:
    event_id = append_event(
        db, EventType.APP_STARTED, {"version": "test", "n": 1}, trace_id="trace-1"
    )
    event = read_event(db, event_id)
    assert event is not None
    assert event["type"] == "AppStarted"
    assert event["trace_id"] == "trace-1"
    assert event["data"] == {"version": "test", "n": 1}
    assert event["ts"]  # timestamp assigned by the database


def test_read_event_missing_returns_none(db: sqlite3.Connection) -> None:
    assert read_event(db, 999_999) is None


def test_transaction_rolls_back_on_error(db: sqlite3.Connection) -> None:
    with pytest.raises(RuntimeError), transaction(db):
        append_event(db, EventType.APP_STARTED, {"inside": "tx"})
        raise RuntimeError("boom")
    count = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert count == 0
