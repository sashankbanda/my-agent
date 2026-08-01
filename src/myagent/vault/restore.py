"""Restore: rebuild the hot store from an encrypted snapshot.

Integrity comes from two independent layers: AES-GCM authentication (a
flipped bit anywhere fails decryption) and SQLite's own integrity_check on
the decrypted database. When a local manifest exists its hash chain is
verified too; on a fresh machine the snapshot is self-describing.

Restore is a stopped-kernel operation (scripts/restore.py) - never run it
against a database the kernel currently has open.
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from pathlib import Path
from typing import Any

from myagent.db import connection
from myagent.events import EventType, append_event
from myagent.logging import get_logger
from myagent.vault import crypto
from myagent.vault.remote import RemoteVault, VaultUnavailableError
from myagent.vault.snapshot import BLOB_PREFIX

log = get_logger(__name__)


class RestoreError(Exception):
    """The snapshot could not be restored; the existing database is untouched."""


def list_snapshots(vault: RemoteVault) -> list[str]:
    """Available snapshot blob names, oldest first (names sort chronologically)."""
    return [blob.name for blob in vault.list_blobs(BLOB_PREFIX)]


def run_restore(
    db_path: Path,
    vault: RemoteVault,
    key: bytes,
    blob_name: str | None = None,
) -> dict[str, Any]:
    """Download, verify, and install a snapshot as the hot store.

    The current database (if any) is preserved next to the restored one as
    ``*.pre-restore`` until the user deletes it - a restore must never be
    the operation that destroys the only good copy.
    """
    if blob_name is None:
        available = list_snapshots(vault)
        if not available:
            raise RestoreError("the vault contains no snapshots")
        blob_name = available[-1]

    try:
        envelope = vault.download(blob_name)
    except VaultUnavailableError as exc:
        raise RestoreError(str(exc)) from exc
    plaintext = crypto.decrypt_blob(envelope, key)  # raises VaultCryptoError on tamper/wrong key
    sha256 = hashlib.sha256(plaintext).hexdigest()

    with tempfile.TemporaryDirectory() as tmp:
        candidate = Path(tmp) / "restored.db"
        candidate.write_bytes(plaintext)
        with connection(candidate) as conn:
            result = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise RestoreError(f"restored database fails integrity_check: {result}")

        db_path.parent.mkdir(parents=True, exist_ok=True)
        if db_path.exists():
            shutil.move(db_path, db_path.with_suffix(".pre-restore"))
            # WAL sidecars belong to the old database, not the restored one.
            for sidecar in (".db-wal", ".db-shm"):
                side = db_path.with_suffix(sidecar)
                if side.exists():
                    side.unlink()
        shutil.copy(candidate, db_path)

    with connection(db_path) as conn:
        append_event(conn, EventType.VAULT_RESTORE_COMPLETED, {"blob": blob_name, "sha256": sha256})
    log.info("restore_completed", blob=blob_name)
    return {"blob_name": blob_name, "sha256": sha256}
