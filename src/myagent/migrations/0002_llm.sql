-- 0002: conversation persistence and gateway state.

CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    title TEXT NOT NULL DEFAULT ''
);

CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions (id),
    role TEXT NOT NULL CHECK (role IN ('system', 'user', 'assistant', 'tool')),
    content TEXT NOT NULL,
    ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    provider TEXT,
    model TEXT,
    tokens INTEGER
);

CREATE INDEX idx_messages_session ON messages (session_id, id);

-- Persisted quota accounting: RPD counts must survive restarts, otherwise a
-- restart would silently burn a whole day's free-tier budget twice.
CREATE TABLE quota_buckets (
    model_key TEXT NOT NULL,
    window TEXT NOT NULL CHECK (window IN ('rpm', 'rpd', 'tpm')),
    count INTEGER NOT NULL DEFAULT 0,
    reset_at REAL NOT NULL,
    PRIMARY KEY (model_key, window)
);

CREATE TABLE provider_health (
    provider TEXT PRIMARY KEY,
    failures INTEGER NOT NULL DEFAULT 0,
    cooldown_until REAL NOT NULL DEFAULT 0
);
