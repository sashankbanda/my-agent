-- 0001: foundation schema.
-- The events table is the append-only log of everything meaningful that
-- happens in the kernel: audit trail, UI live feed, and debugging record.
-- Rows are inserted, never updated or deleted by application code.

CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    type TEXT NOT NULL,
    trace_id TEXT,
    data_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX idx_events_ts ON events (ts);
CREATE INDEX idx_events_type ON events (type);
CREATE INDEX idx_events_trace ON events (trace_id) WHERE trace_id IS NOT NULL;
