-- 0003: memory layer.
--
-- memory_items carries provenance/confidence/privacy columns from day one
-- (columns are cheap; the consolidation machinery that uses them arrives in
-- M8). messages_fts mirrors messages for keyword retrieval; triggers keep it
-- in sync automatically.

CREATE TABLE memory_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL DEFAULT 'fact' CHECK (type IN ('fact', 'preference', 'procedure')),
    content TEXT NOT NULL,
    provenance TEXT NOT NULL DEFAULT 'user',
    confidence REAL NOT NULL DEFAULT 1.0,
    privacy_class TEXT NOT NULL DEFAULT 'cloud_ok' CHECK (privacy_class IN ('cloud_ok', 'local_only')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE VIRTUAL TABLE messages_fts USING fts5(
    content,
    content='messages',
    content_rowid='id'
);

CREATE TRIGGER messages_fts_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts (rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER messages_fts_delete AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts (messages_fts, rowid, content) VALUES ('delete', old.id, old.content);
END;

-- Backfill any messages that existed before this migration.
INSERT INTO messages_fts (rowid, content) SELECT id, content FROM messages;
