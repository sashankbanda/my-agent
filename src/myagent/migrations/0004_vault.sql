-- 0004: vault manifest.
--
-- One row per snapshot uploaded to the remote vault. hash chains to the
-- previous row so corruption or tampering is detectable at backup time.
-- The manifest travels inside every snapshot (it is a table in the same
-- database), making each snapshot self-describing at restore time.

CREATE TABLE vault_manifest (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    blob_name TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL,
    prev_sha256 TEXT,
    size_bytes INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
