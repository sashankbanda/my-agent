"""Vault tests: FolderVault, snapshots, retention, and the restore drill.

The restore drill is Milestone 2's exit gate: seed -> snapshot -> wipe ->
restore with only the key -> identical memory state.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from myagent.config import Settings, VaultSettings
from myagent.core import history
from myagent.db import connection
from myagent.memory import store
from myagent.vault import crypto, restore, snapshot
from myagent.vault.remote import FolderVault, VaultUnavailableError


@pytest.fixture
def vault(tmp_path: Path) -> FolderVault:
    return FolderVault(tmp_path / "vault")


@pytest.fixture
def key() -> bytes:
    return os.urandom(32)


@pytest.fixture
def vault_settings() -> VaultSettings:
    return VaultSettings(enabled=True, backend="folder", keep_daily=3, keep_monthly=2)


def test_folder_vault_round_trip(vault: FolderVault) -> None:
    vault.upload("snapshots/a.snap", b"alpha")
    vault.upload("snapshots/b.snap", b"beta")
    assert vault.download("snapshots/a.snap") == b"alpha"
    names = [blob.name for blob in vault.list_blobs("snapshots/")]
    assert names == ["snapshots/a.snap", "snapshots/b.snap"]
    vault.delete("snapshots/a.snap")
    assert [blob.name for blob in vault.list_blobs()] == ["snapshots/b.snap"]
    with pytest.raises(VaultUnavailableError):
        vault.download("snapshots/a.snap")


def test_folder_vault_blocks_path_escape(vault: FolderVault) -> None:
    with pytest.raises(ValueError, match="escapes the vault root"):
        vault.upload("../outside.snap", b"nope")


def test_snapshot_uploads_and_records_manifest(
    db: sqlite3.Connection,
    settings: Settings,
    vault: FolderVault,
    vault_settings: VaultSettings,
    key: bytes,
) -> None:
    store.add_fact(settings.db_path(), "snapshot me")
    entry = snapshot.run_snapshot(settings.db_path(), vault, vault_settings, key)
    blobs = vault.list_blobs(snapshot.BLOB_PREFIX)
    assert [blob.name for blob in blobs] == [entry["blob_name"]]
    last = snapshot.last_snapshot(settings.db_path())
    assert last is not None
    assert last["blob_name"] == entry["blob_name"]
    assert snapshot.verify_manifest_chain(settings.db_path()) is True


def test_vacuum_copy_is_consistent_while_writer_is_active(
    db: sqlite3.Connection,
    settings: Settings,
) -> None:
    """The snapshot's VACUUM INTO copy excludes uncommitted concurrent writes.

    (The manifest write itself needs the writer to have committed - WAL allows
    one writer at a time - which matches production: the kernel only ever
    holds short transactions. Here we prove the *copy mechanism* is safe.)
    """
    session = history.create_session(settings.db_path())
    db.execute("BEGIN")
    db.execute(
        "INSERT INTO messages (session_id, role, content) VALUES (?, 'user', 'uncommitted')",
        (session,),
    )
    copy = Path(settings.db_path()).parent / "copy-check.db"
    with connection(settings.db_path()) as reader:
        reader.execute("VACUUM INTO ?", (str(copy),))
    db.execute("COMMIT")
    with connection(copy) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        # The uncommitted write must not be in the snapshot copy.
        count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        assert count == 0


def test_manifest_chain_links_snapshots(
    db: sqlite3.Connection,
    settings: Settings,
    vault: FolderVault,
    vault_settings: VaultSettings,
    key: bytes,
) -> None:
    first = snapshot.run_snapshot(
        settings.db_path(), vault, vault_settings, key, now=datetime(2026, 8, 1, tzinfo=UTC)
    )
    snapshot.run_snapshot(
        settings.db_path(), vault, vault_settings, key, now=datetime(2026, 8, 2, tzinfo=UTC)
    )
    with connection(settings.db_path()) as conn:
        rows = conn.execute("SELECT sha256, prev_sha256 FROM vault_manifest ORDER BY id").fetchall()
    assert rows[0]["prev_sha256"] is None
    assert rows[1]["prev_sha256"] == first["sha256"]
    assert snapshot.verify_manifest_chain(settings.db_path()) is True


def test_retention_keeps_daily_and_monthly(
    db: sqlite3.Connection,
    settings: Settings,
    vault: FolderVault,
    vault_settings: VaultSettings,
    key: bytes,
) -> None:
    """keep_daily=3, keep_monthly=2: old dailies vanish, month-firsts survive."""
    days = [
        datetime(2026, 6, 1, tzinfo=UTC),
        datetime(2026, 6, 15, tzinfo=UTC),
        datetime(2026, 7, 1, tzinfo=UTC),
        datetime(2026, 7, 20, tzinfo=UTC),
        datetime(2026, 8, 1, tzinfo=UTC),
        datetime(2026, 8, 2, tzinfo=UTC),
        datetime(2026, 8, 3, tzinfo=UTC),
    ]
    for day in days:
        snapshot.run_snapshot(settings.db_path(), vault, vault_settings, key, now=day)
    names = [blob.name for blob in vault.list_blobs(snapshot.BLOB_PREFIX)]
    # Last 3 dailies:
    assert any("20260801" in name for name in names)
    assert any("20260802" in name for name in names)
    assert any("20260803" in name for name in names)
    # First snapshot of the last 2 months (July, August):
    assert any("20260701" in name for name in names)
    # June snapshots fall outside both policies:
    assert not any("202606" in name for name in names)


def test_restore_drill_reproduces_state(
    db: sqlite3.Connection,
    settings: Settings,
    tmp_path: Path,
    vault: FolderVault,
    vault_settings: VaultSettings,
    key: bytes,
) -> None:
    """M2 exit gate: wipe everything except the vault + key, restore, compare."""
    session = history.create_session(settings.db_path())
    history.append_message(settings.db_path(), session, "user", "remember the drill")
    store.add_fact(settings.db_path(), "the drill is real")
    expected_facts = store.list_facts(settings.db_path())
    expected_messages = history.get_messages(settings.db_path(), session)

    snapshot.run_snapshot(settings.db_path(), vault, vault_settings, key)

    fresh_db = tmp_path / "fresh-machine" / "myagent.db"  # simulated new machine
    result = restore.run_restore(fresh_db, vault, key)

    assert store.list_facts(fresh_db) == expected_facts
    assert history.get_messages(fresh_db, session) == expected_messages
    assert snapshot.verify_manifest_chain(fresh_db) is True
    assert result["blob_name"].startswith(snapshot.BLOB_PREFIX)


def test_restore_picks_latest_and_honors_explicit_blob(
    db: sqlite3.Connection,
    settings: Settings,
    tmp_path: Path,
    vault: FolderVault,
    vault_settings: VaultSettings,
    key: bytes,
) -> None:
    store.add_fact(settings.db_path(), "state one")
    first = snapshot.run_snapshot(
        settings.db_path(), vault, vault_settings, key, now=datetime(2026, 8, 1, tzinfo=UTC)
    )
    store.add_fact(settings.db_path(), "state two")
    snapshot.run_snapshot(
        settings.db_path(), vault, vault_settings, key, now=datetime(2026, 8, 2, tzinfo=UTC)
    )

    latest_db = tmp_path / "latest.db"
    restore.run_restore(latest_db, vault, key)
    assert len(store.list_facts(latest_db)) == 2

    older_db = tmp_path / "older.db"
    restore.run_restore(older_db, vault, key, blob_name=first["blob_name"])
    assert len(store.list_facts(older_db)) == 1


def test_restore_preserves_previous_database(
    db: sqlite3.Connection,
    settings: Settings,
    vault: FolderVault,
    vault_settings: VaultSettings,
    key: bytes,
) -> None:
    snapshot.run_snapshot(settings.db_path(), vault, vault_settings, key)
    # Restore is a stopped-kernel operation: no open handles on the database
    # (Windows cannot move an open file, and that is the correct constraint).
    db.close()
    restore.run_restore(settings.db_path(), vault, key)
    assert settings.db_path().with_suffix(".pre-restore").exists()


def test_restore_wrong_key_leaves_database_untouched(
    db: sqlite3.Connection,
    settings: Settings,
    vault: FolderVault,
    vault_settings: VaultSettings,
    key: bytes,
) -> None:
    store.add_fact(settings.db_path(), "must survive")
    snapshot.run_snapshot(settings.db_path(), vault, vault_settings, key)
    with pytest.raises(crypto.VaultCryptoError):
        restore.run_restore(settings.db_path(), vault, os.urandom(32))
    assert len(store.list_facts(settings.db_path())) == 1


def test_restore_empty_vault_raises(settings: Settings, vault: FolderVault, key: bytes) -> None:
    with pytest.raises(restore.RestoreError, match="no snapshots"):
        restore.run_restore(settings.db_path(), vault, key)
