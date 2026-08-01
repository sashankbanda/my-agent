"""SQLite access and migrations.

One database file (WAL mode) is the operational truth. Connections are cheap
and short-lived: open one via ``connection()``/``transaction()``, use it, and
let the context manager close it. Pragmas are applied on *every* fresh
connection - WAL is a property of the file but busy_timeout and foreign_keys
are per-connection.

Migrations are numbered ``NNNN_name.sql`` files applied strictly in order and
never edited after commit; ``schema_version`` records what has been applied,
which makes ``migrate()`` idempotent.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from myagent.logging import get_logger

log = get_logger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_MIGRATION_FILE_PATTERN = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def connect(db_path: Path) -> sqlite3.Connection:
    """Open a connection with the required pragmas applied.

    The parent directory is created on demand so a fresh install boots
    without a setup step.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, autocommit=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def connection(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Yield a fresh connection and always close it."""
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block atomically: BEGIN, then COMMIT, or ROLLBACK on any error."""
    conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _discover_migrations(migrations_dir: Path) -> list[tuple[int, Path]]:
    """Return (version, path) pairs for well-formed migration files, ordered."""
    found: list[tuple[int, Path]] = []
    for path in migrations_dir.iterdir():
        match = _MIGRATION_FILE_PATTERN.match(path.name)
        if match:
            found.append((int(match.group(1)), path))
    found.sort(key=lambda pair: pair[0])
    return found


def migrate(conn: sqlite3.Connection, migrations_dir: Path = MIGRATIONS_DIR) -> list[int]:
    """Apply all unapplied migrations in order; return the versions applied.

    Each migration runs inside its own transaction together with its
    ``schema_version`` record, so a failed migration leaves the database at
    the previous version, not in between.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """
    )
    applied = {row["version"] for row in conn.execute("SELECT version FROM schema_version")}
    newly_applied: list[int] = []
    for version, path in _discover_migrations(migrations_dir):
        if version in applied:
            continue
        sql = path.read_text(encoding="utf-8")
        with transaction(conn):
            conn.executescript(sql)
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
        newly_applied.append(version)
        log.info("migration_applied", version=version, file=path.name)
    return newly_applied
