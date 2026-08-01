"""Minimal background scheduling: the nightly vault snapshot.

Deliberately tiny (v3 review): one asyncio task that sleeps until the
configured hour and runs the snapshot in a worker thread. The full poller
scheduler (M5) replaces this module; keeping it separate makes that
replacement a file deletion, not surgery.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from myagent.config import Settings
from myagent.logging import get_logger
from myagent.vault import crypto, snapshot
from myagent.vault.remote import VaultUnavailableError, make_vault

log = get_logger(__name__)

RETRY_DELAY_SECONDS = 30 * 60  # a failed nightly snapshot retries in half an hour


def run_snapshot_now(settings: Settings, db_path: Path) -> dict[str, object]:
    """Synchronous snapshot entry used by the API endpoint and the nightly task.

    First-ever use creates the vault key; the recovery string is returned so
    the caller (API/UI) can show it to the user exactly once.
    """
    vault = make_vault(settings)  # raises VaultUnavailableError if unconfigured
    key, recovery = crypto.get_or_create_key()
    entry = snapshot.run_snapshot(db_path, vault, settings.vault, key)
    if recovery is not None:
        entry["recovery_string"] = recovery
    return entry


async def nightly_snapshots(settings: Settings, db_path: Path) -> None:
    """Run forever: snapshot at the configured hour, retry on failure."""
    while True:
        delay = snapshot.seconds_until_hour(settings.vault.snapshot_hour)
        log.info("snapshot_scheduled", in_seconds=int(delay))
        await asyncio.sleep(delay)
        try:
            await asyncio.to_thread(run_snapshot_now, settings, db_path)
        except VaultUnavailableError as exc:
            log.warning("snapshot_skipped", reason=str(exc))
        except Exception:
            log.exception("snapshot_failed")
            await asyncio.sleep(RETRY_DELAY_SECONDS)
