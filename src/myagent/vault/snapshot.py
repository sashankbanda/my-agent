"""Snapshots: consistent, encrypted, retained copies of the hot store.

``VACUUM INTO`` produces a consistent point-in-time copy without blocking
writers - the one safe way to copy a live SQLite database. The copy is
compressed, encrypted, uploaded, recorded in the hash-chained manifest, and
old snapshots are pruned by the retention policy (keep the last N daily and
the first snapshot of each month for M months).

Each snapshot contains the manifest table describing all snapshots before
it, so any single snapshot is self-describing at restore time.
"""

from __future__ import annotations

import hashlib
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from myagent.config import VaultSettings
from myagent.db import connection, transaction
from myagent.events import EventType, append_event
from myagent.logging import get_logger
from myagent.vault import crypto
from myagent.vault.remote import RemoteVault

log = get_logger(__name__)

BLOB_PREFIX = "snapshots/"
BLOB_SUFFIX = ".snap"


def _blob_name(when: datetime) -> str:
    # Microseconds keep names unique even for back-to-back manual backups.
    return f"{BLOB_PREFIX}myagent-{when.strftime('%Y%m%d-%H%M%S-%f')}{BLOB_SUFFIX}"


def _blob_date(name: str) -> datetime | None:
    """Parse the timestamp back out of a blob name; None for foreign blobs."""
    stem = name.removeprefix(BLOB_PREFIX).removesuffix(BLOB_SUFFIX)
    try:
        return datetime.strptime(stem, "myagent-%Y%m%d-%H%M%S-%f").replace(tzinfo=UTC)
    except ValueError:
        return None


def run_snapshot(
    db_path: Path,
    vault: RemoteVault,
    settings: VaultSettings,
    key: bytes,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Create, upload, record, and prune; returns the new manifest entry."""
    when = now or datetime.now(UTC)
    blob_name = _blob_name(when)

    with tempfile.TemporaryDirectory() as tmp:
        copy_path = Path(tmp) / "snapshot.db"
        with connection(db_path) as conn:
            # VACUUM INTO requires a path that does not exist yet.
            conn.execute("VACUUM INTO ?", (str(copy_path),))
        plaintext = copy_path.read_bytes()

    sha256 = hashlib.sha256(plaintext).hexdigest()
    envelope = crypto.encrypt_blob(plaintext, key)
    vault.upload(blob_name, envelope)

    with connection(db_path) as conn, transaction(conn):
        prev = conn.execute("SELECT sha256 FROM vault_manifest ORDER BY id DESC LIMIT 1").fetchone()
        conn.execute(
            """
            INSERT INTO vault_manifest (blob_name, sha256, prev_sha256, size_bytes)
            VALUES (?, ?, ?, ?)
            """,
            (blob_name, sha256, prev["sha256"] if prev else None, len(envelope)),
        )
        append_event(
            conn,
            EventType.VAULT_SNAPSHOT_CREATED,
            {"blob": blob_name, "sha256": sha256, "size": len(envelope)},
        )

    pruned = prune(vault, settings, when)
    log.info("snapshot_created", blob=blob_name, size=len(envelope), pruned=len(pruned))
    return {"blob_name": blob_name, "sha256": sha256, "size_bytes": len(envelope)}


def prune(vault: RemoteVault, settings: VaultSettings, now: datetime) -> list[str]:
    """Apply retention: keep the last ``keep_daily`` snapshots plus the first
    snapshot of each of the last ``keep_monthly`` months; delete the rest."""
    dated = [
        (name, stamp)
        for name, stamp in (
            (blob.name, _blob_date(blob.name)) for blob in vault.list_blobs(BLOB_PREFIX)
        )
        if stamp is not None
    ]
    dated.sort(key=lambda pair: pair[1])

    keep: set[str] = set()
    keep.update(name for name, _ in dated[-settings.keep_daily :])
    first_of_month: dict[str, str] = {}
    for name, stamp in dated:
        first_of_month.setdefault(stamp.strftime("%Y-%m"), name)
    months = sorted(first_of_month)[-settings.keep_monthly :]
    keep.update(first_of_month[month] for month in months)

    removed: list[str] = []
    for name, _ in dated:
        if name not in keep:
            vault.delete(name)
            removed.append(name)
    return removed


def verify_manifest_chain(db_path: Path) -> bool:
    """True if every manifest row's prev hash matches its predecessor."""
    with connection(db_path) as conn:
        rows = conn.execute("SELECT sha256, prev_sha256 FROM vault_manifest ORDER BY id").fetchall()
    previous: str | None = None
    for row in rows:
        if row["prev_sha256"] != previous:
            return False
        previous = row["sha256"]
    return True


def last_snapshot(db_path: Path) -> dict[str, Any] | None:
    """Most recent manifest entry, for the status endpoint."""
    with connection(db_path) as conn:
        row = conn.execute(
            """
            SELECT blob_name, sha256, size_bytes, created_at
            FROM vault_manifest ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
    return dict(row) if row else None


def seconds_until_hour(hour: int, now_ts: float | None = None) -> float:
    """Seconds until the next local occurrence of ``hour``:00."""
    now = datetime.fromtimestamp(now_ts if now_ts is not None else time.time()).astimezone()
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()
