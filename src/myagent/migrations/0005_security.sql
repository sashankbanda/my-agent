-- 0005: permission grants.
--
-- One row per standing permission decision. Scope semantics:
--   session -> valid while that session lives (session_id set)
--   always  -> valid until revoked (session_id NULL)
-- "once" decisions are never stored: they apply to a single call only.
--
-- The audit trail is NOT a table here: it is a view over the append-only
-- events log (v3 review F11 - one log, not two). Revocations are also
-- recorded as events, so the log stays the complete history.

CREATE TABLE grants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tool TEXT NOT NULL,
    scope TEXT NOT NULL CHECK (scope IN ('session', 'always')),
    session_id TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (tool, scope, session_id)
);

CREATE INDEX idx_grants_tool ON grants (tool);
