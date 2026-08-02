"""Disaster recovery: restore the MyAgent database from the vault.

Run with the kernel STOPPED.

Usage:
    uv run python scripts/restore.py --auth            # one-time Google Drive consent
    uv run python scripts/restore.py --backup          # manual snapshot right now
    uv run python scripts/restore.py --list            # show available snapshots
    uv run python scripts/restore.py                   # restore the latest snapshot
    uv run python scripts/restore.py --blob NAME       # restore a specific snapshot

Fresh-machine flow: install, configure the vault backend in config, run
--auth (drive backend), then run this script - it will ask for the recovery
string you saved at setup and rebuild everything.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from myagent.config import load_settings
from myagent.scheduler import run_snapshot_now
from myagent.vault import crypto, restore
from myagent.vault.remote import make_vault


def ensure_key() -> bytes:
    """Load the vault key, asking for the recovery string on a fresh machine."""
    key = crypto.load_key()
    if key is not None:
        return key
    print("No vault key on this machine. Enter the recovery string you saved at setup.")
    recovery = getpass.getpass("Recovery string (input hidden): ").strip()
    return crypto.install_key_from_recovery(recovery)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--auth", action="store_true", help="authorize Google Drive")
    parser.add_argument("--backup", action="store_true", help="snapshot now")
    parser.add_argument("--list", action="store_true", help="list snapshots")
    parser.add_argument("--blob", metavar="NAME", help="specific snapshot to restore")
    args = parser.parse_args()

    settings = load_settings()

    if args.auth:
        from myagent.vault.drive import authorize_interactively

        authorize_interactively(settings.vault.client_secrets)
        print("Google Drive authorized; token stored in the Credential Manager.")
        return 0

    if args.backup:
        entry = run_snapshot_now(settings, settings.db_path())
        print(f"snapshot uploaded: {entry['blob_name']} ({entry['size_bytes']} bytes)")
        if "recovery_string" in entry:
            print(
                "\nFIRST BACKUP - your recovery string (shown ONCE, store it OFF this machine):\n"
                f"\n    {entry['recovery_string']}\n"
                "\nWithout it, losing this machine means losing the vault."
            )
        return 0

    vault = make_vault(settings)

    if args.list:
        names = restore.list_snapshots(vault)
        if not names:
            print("the vault contains no snapshots")
        for name in names:
            print(f"  {name}")
        return 0

    key = ensure_key()
    print(f"restoring into: {settings.db_path()}")
    print("make sure the kernel is NOT running.")
    result = restore.run_restore(settings.db_path(), vault, key, blob_name=args.blob)
    print(f"restored {result['blob_name']} (sha256 {result['sha256'][:16]}...)")
    print("previous database (if any) kept as *.pre-restore next to it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
